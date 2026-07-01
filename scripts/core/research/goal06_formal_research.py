from __future__ import annotations

from dataclasses import dataclass
from typing import Protocol

from scripts.core.persistence.goal01_store import PersistenceStore, content_hash


class ResearchBoundaryError(RuntimeError):
    pass


BLOCKED_SOURCE_TYPES = frozenset({"video", "audio", "asr", "comment", "video_analysis"})
BLOCKED_PLATFORMS = frozenset({"douyin", "tiktok", "bilibili", "xiaohongshu", "kuaishou"})


@dataclass(frozen=True)
class ResearchQuery:
    topic_id: str
    query: str
    purpose: str = "formal_topic_research"


@dataclass(frozen=True)
class SearchResult:
    result_id: str
    title: str
    url: str
    provider: str
    source_type: str = "web_page"
    platform: str | None = None
    snippet: str = ""
    metadata: dict[str, object] | None = None


@dataclass(frozen=True)
class FetchedDocument:
    result_id: str
    url: str
    title: str
    text: str
    fetched_at: str
    fetcher: str
    source_type: str = "web_page"
    platform: str | None = None
    metadata: dict[str, object] | None = None


@dataclass(frozen=True)
class ExtractedEvidence:
    evidence_id: str
    result_id: str
    claim: str
    quote: str
    locator: dict[str, object]
    extractor: str
    metadata: dict[str, object] | None = None


@dataclass(frozen=True)
class ResearchRunResult:
    plan_version_id: str
    artifact_version_id: str
    source_version_ids: tuple[str, ...]
    fetch_version_ids: tuple[str, ...]
    evidence_version_ids: tuple[str, ...]


class SearchProvider(Protocol):
    provider_name: str

    def search(self, query: ResearchQuery) -> list[SearchResult]:
        ...


class ResearchFetcher(Protocol):
    fetcher_name: str

    def fetch(self, result: SearchResult) -> FetchedDocument:
        ...


class ResearchExtractor(Protocol):
    extractor_name: str

    def extract(self, document: FetchedDocument) -> list[ExtractedEvidence]:
        ...


class FormalResearchMaterializer:
    def __init__(self, store: PersistenceStore):
        self.store = store

    def persist_run(
        self,
        *,
        query: ResearchQuery,
        provider_name: str,
        fetcher_name: str,
        extractor_name: str,
        search_results: list[SearchResult],
        documents: list[FetchedDocument],
        evidence: list[ExtractedEvidence],
    ) -> ResearchRunResult:
        self._validate_query(query)
        for result in search_results:
            self._validate_source(result.source_type, result.platform, result.url)
        for document in documents:
            self._validate_source(document.source_type, document.platform, document.url)

        plan_root = self.store.create_root("research_plan")
        plan_payload = {
            "goal": "GOAL-06",
            "topic_id": query.topic_id,
            "query": query.query,
            "purpose": query.purpose,
            "provider": provider_name,
            "fetcher": fetcher_name,
            "extractor": extractor_name,
        }
        plan_version = self.store.append_version(plan_root, plan_payload, projection_version="goal06.research_plan.v1")
        self.store.set_current_version(plan_root, plan_version)

        source_versions: list[str] = []
        source_refs_by_result: dict[str, tuple[str, str]] = {}
        for result in search_results:
            source_root = self.store.create_root("research_source")
            payload = {
                "result_id": result.result_id,
                "title": result.title,
                "url": result.url,
                "provider": result.provider,
                "source_type": result.source_type,
                "platform": result.platform,
                "snippet": result.snippet,
                "metadata": result.metadata or {},
            }
            version = self.store.append_version(source_root, payload, projection_version="goal06.research_source.v1")
            self.store.set_current_version(source_root, version)
            self.store.record_object_reference(
                source_version_id=plan_version,
                relation_role="found_source",
                target_object_kind="research_source",
                target_stable_id=source_root,
                target_version_id=version,
                target_content_hash=content_hash(payload, "goal06.research_source.v1"),
                locator={"result_id": result.result_id, "url": result.url},
            )
            source_versions.append(version)
            source_refs_by_result[result.result_id] = (source_root, version)

        fetch_versions: list[str] = []
        fetch_refs_by_result: dict[str, tuple[str, str]] = {}
        for document in documents:
            source_ref = source_refs_by_result.get(document.result_id)
            if source_ref is None:
                raise ResearchBoundaryError(f"fetched document has no source result: {document.result_id}")
            fetch_root = self.store.create_root("research_fetch")
            payload = {
                "result_id": document.result_id,
                "url": document.url,
                "title": document.title,
                "text": document.text,
                "fetched_at": document.fetched_at,
                "fetcher": document.fetcher,
                "source_type": document.source_type,
                "platform": document.platform,
                "metadata": document.metadata or {},
            }
            version = self.store.append_version(
                fetch_root,
                payload,
                projection_version="goal06.research_fetch.v1",
                business_payload={"result_id": document.result_id, "url": document.url, "text": document.text},
            )
            self.store.set_current_version(fetch_root, version)
            self.store.record_object_reference(
                source_version_id=version,
                relation_role="fetched_from",
                target_object_kind="research_source",
                target_stable_id=source_ref[0],
                target_version_id=source_ref[1],
                target_content_hash=None,
                locator={"result_id": document.result_id, "url": document.url},
            )
            fetch_versions.append(version)
            fetch_refs_by_result[document.result_id] = (fetch_root, version)

        evidence_versions: list[str] = []
        for item in evidence:
            fetch_ref = fetch_refs_by_result.get(item.result_id)
            if fetch_ref is None:
                raise ResearchBoundaryError(f"evidence has no fetched document: {item.evidence_id}")
            evidence_root = self.store.create_root("research_evidence")
            payload = {
                "evidence_id": item.evidence_id,
                "result_id": item.result_id,
                "claim": item.claim,
                "quote": item.quote,
                "locator": item.locator,
                "extractor": item.extractor,
                "metadata": item.metadata or {},
            }
            version = self.store.append_version(evidence_root, payload, projection_version="goal06.research_evidence.v1")
            self.store.set_current_version(evidence_root, version)
            self.store.record_object_reference(
                source_version_id=version,
                relation_role="extracted_from",
                target_object_kind="research_fetch",
                target_stable_id=fetch_ref[0],
                target_version_id=fetch_ref[1],
                target_content_hash=None,
                locator=item.locator,
            )
            self.store.record_object_reference(
                source_version_id=plan_version,
                relation_role="uses_evidence",
                target_object_kind="research_evidence",
                target_stable_id=evidence_root,
                target_version_id=version,
                target_content_hash=content_hash(payload, "goal06.research_evidence.v1"),
                locator=item.locator,
            )
            evidence_versions.append(version)

        artifact_root = self.store.create_root("research_artifact")
        artifact_payload = {
            "topic_id": query.topic_id,
            "query": query.query,
            "source_count": len(source_versions),
            "fetch_count": len(fetch_versions),
            "evidence_count": len(evidence_versions),
            "source_version_ids": source_versions,
            "evidence_version_ids": evidence_versions,
        }
        artifact_version = self.store.append_version(
            artifact_root,
            artifact_payload,
            projection_version="goal06.research_artifact.v1",
            business_payload={"topic_id": query.topic_id, "query": query.query},
        )
        self.store.set_current_version(artifact_root, artifact_version)
        self.store.record_object_reference(
            source_version_id=artifact_version,
            relation_role="based_on_plan",
            target_object_kind="research_plan",
            target_stable_id=plan_root,
            target_version_id=plan_version,
            target_content_hash=content_hash(plan_payload, "goal06.research_plan.v1"),
            locator={"topic_id": query.topic_id, "query": query.query},
        )
        self.store.record_audit(
            event_type="goal06.formal_research.materialized",
            actor="formal_research_materializer",
            object_kind="research_artifact",
            object_id=artifact_root,
            version_id=artifact_version,
            payload={
                "topic_id": query.topic_id,
                "source_count": len(source_versions),
                "fetch_count": len(fetch_versions),
                "evidence_count": len(evidence_versions),
            },
            correlation_id=plan_root,
        )
        return ResearchRunResult(
            plan_version_id=plan_version,
            artifact_version_id=artifact_version,
            source_version_ids=tuple(source_versions),
            fetch_version_ids=tuple(fetch_versions),
            evidence_version_ids=tuple(evidence_versions),
        )

    @staticmethod
    def _validate_query(query: ResearchQuery) -> None:
        if not query.topic_id:
            raise ResearchBoundaryError("topic_id is required")
        if not query.query:
            raise ResearchBoundaryError("query is required")

    @staticmethod
    def _validate_source(source_type: str, platform: str | None, url: str) -> None:
        normalized_type = source_type.lower()
        normalized_platform = (platform or "").lower()
        normalized_url = url.lower()
        if normalized_type in BLOCKED_SOURCE_TYPES:
            raise ResearchBoundaryError(f"formal research cannot use {source_type} source")
        if normalized_platform in BLOCKED_PLATFORMS:
            raise ResearchBoundaryError(f"formal research cannot use {platform} platform")
        if any(host in normalized_url for host in ("douyin.com", "tiktok.com", "bilibili.com", "xiaohongshu.com")):
            raise ResearchBoundaryError("formal research cannot use video-platform url")


class FormalResearchService:
    def __init__(
        self,
        *,
        provider: SearchProvider,
        fetcher: ResearchFetcher,
        extractor: ResearchExtractor,
        materializer: FormalResearchMaterializer,
    ):
        self.provider = provider
        self.fetcher = fetcher
        self.extractor = extractor
        self.materializer = materializer

    def run(self, query: ResearchQuery) -> ResearchRunResult:
        results = self.provider.search(query)
        documents = [self.fetcher.fetch(result) for result in results]
        evidence: list[ExtractedEvidence] = []
        for document in documents:
            evidence.extend(self.extractor.extract(document))
        return self.materializer.persist_run(
            query=query,
            provider_name=self.provider.provider_name,
            fetcher_name=self.fetcher.fetcher_name,
            extractor_name=self.extractor.extractor_name,
            search_results=results,
            documents=documents,
            evidence=evidence,
        )
