Produce only a structured content plan from the frozen input. Do not fetch, search, publish, write data, change state, or add unsupported facts. Return exactly document, source_boundaries, unresolved. document must be a non-empty structured object. source_boundaries and unresolved must be arrays of plain strings. Use Simplified Chinese.

The frozen input may contain confirmed experience cards in considered_experience. They are references, not universal rules. Read each card's experience_layer, trigger_signals, applicable_when, use_positions, method, boundary and not_applicable_when. Choose an experience only when the current topic and the supplied research show its trigger condition. Use at most one structure card for the whole plan; add section or local-detail cards only when a specific opening, body, transition, ending or sentence problem calls for them. The plan must state which experience is used, where it is used, and why; it must also state why supplied but irrelevant experiences are not used. Do not force every card into the plan.

Frozen input:
{input_assembly}
