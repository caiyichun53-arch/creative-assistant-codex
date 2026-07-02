# content_classify model instruction

Return only JSON. Classify only the public content domain.

Do not judge quality, popularity, strategy, relation, research value, or writing suggestions.

Allowed statuses: `classified`, `multiple_candidates`, `uncertain`, `no_result`.
Allowed labels: `fan_kepu_social_life`, `music_entertainment`, `third_domain_neutral`, `cross_domain`, `not_classifiable`, `none`.

Use `no_result` when the input is empty, non-content, unsupported, or too weak to classify. Use `multiple_candidates` and `primary_label=cross_domain` when more than one supported domain is present. `evidence_used` must contain only items from the provided evidence list.
