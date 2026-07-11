"""Single source of truth for the domain_label enum used across every
business Skill binding (source_to_topic/content_plan/script_generate/
script_review/sample_deep_analyze/tactic_extract).

2026-07-13 (置顶规则总表核对后): this exact frozenset used to be copy-pasted
identically into 5 separate run_*.py files, discovered while auditing the
pinned-rules doc against the real catalog. Not itself a BUSINESS_RULE_CATALOG.yaml
requirement -- domain_label's real values come from BR-DOMAIN config
(config/domains/*.yaml sets formal_domain_label per domain) -- this module
just stops the same literal set from drifting across 5+ copies.
"""

from __future__ import annotations

ALLOWED_DOMAIN_LABELS = frozenset(
    {"fan_kepu_social_life", "music_entertainment", "third_domain_neutral", "cross_domain", "unknown"}
)
