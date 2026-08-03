"""Formal research ports and materialization primitives for GOAL-06."""

from .goal06_formal_research import (
    ExtractedEvidence,
    FetchedDocument,
    FormalResearchMaterializer,
    FormalResearchService,
    ResearchBoundaryError,
    ResearchExtractor,
    ResearchFetcher,
    ResearchQuery,
    ResearchRunResult,
    SearchProvider,
    SearchResult,
    make_formal_research_runtime_handler,
)

__all__ = [
    "ExtractedEvidence",
    "FetchedDocument",
    "FormalResearchMaterializer",
    "FormalResearchService",
    "ResearchBoundaryError",
    "ResearchExtractor",
    "ResearchFetcher",
    "ResearchQuery",
    "ResearchRunResult",
    "SearchProvider",
    "SearchResult",
    "make_formal_research_runtime_handler",
]
