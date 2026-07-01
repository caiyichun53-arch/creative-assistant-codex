"""Formal research ports and materialization primitives for GOAL-06."""

from .goal06_formal_research import (
    ExtractedEvidence,
    FetchedDocument,
    FormalResearchMaterializer,
    FormalResearchService,
    GOAL06_TOPIC_FIRST_JOB_KIND,
    GOAL06_TOPIC_FIRST_WORKFLOW,
    ResearchBoundaryError,
    ResearchExtractor,
    ResearchFetcher,
    ResearchQuery,
    ResearchRunResult,
    SearchProvider,
    SearchResult,
    make_formal_research_runtime_handler,
    start_topic_first_research_workflow,
)

__all__ = [
    "ExtractedEvidence",
    "FetchedDocument",
    "FormalResearchMaterializer",
    "FormalResearchService",
    "GOAL06_TOPIC_FIRST_JOB_KIND",
    "GOAL06_TOPIC_FIRST_WORKFLOW",
    "ResearchBoundaryError",
    "ResearchExtractor",
    "ResearchFetcher",
    "ResearchQuery",
    "ResearchRunResult",
    "SearchProvider",
    "SearchResult",
    "make_formal_research_runtime_handler",
    "start_topic_first_research_workflow",
]
