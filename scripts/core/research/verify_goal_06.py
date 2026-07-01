from __future__ import annotations

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[3]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from scripts.core.persistence.goal01_store import PersistenceStore, UUIDv7Generator
from scripts.core.runtime.goal04_runtime_host import RuntimeHandlerContract, RuntimeHost
from scripts.core.scheduler.goal03_scheduler import Goal03Scheduler
from scripts.core.research.goal06_formal_research import (
    ExtractedEvidence,
    FetchedDocument,
    FormalResearchMaterializer,
    FormalResearchService,
    GOAL06_TOPIC_FIRST_JOB_KIND,
    ResearchBoundaryError,
    ResearchQuery,
    SearchResult,
    make_formal_research_runtime_handler,
    start_topic_first_research_workflow,
)
from scripts.core.workflow.goal05_workflow import Goal05WorkflowOrchestrator


class FakeSearchProvider:
    provider_name = "fake-search-provider"

    def __init__(self, results: list[SearchResult]):
        self.results = results

    def search(self, query: ResearchQuery) -> list[SearchResult]:
        if query.purpose != "formal_topic_research":
            raise AssertionError("unexpected research purpose")
        return list(self.results)


class ReplayFetcher:
    fetcher_name = "replay-fetcher"

    def __init__(self, documents: dict[str, FetchedDocument]):
        self.documents = documents

    def fetch(self, result: SearchResult) -> FetchedDocument:
        return self.documents[result.result_id]


class FixtureExtractor:
    extractor_name = "fixture-extractor"

    def extract(self, document: FetchedDocument) -> list[ExtractedEvidence]:
        return [
            ExtractedEvidence(
                evidence_id=f"ev-{document.result_id}",
                result_id=document.result_id,
                claim=f"{document.title} contains fixture evidence",
                quote=document.text[:80],
                locator={"url": document.url, "offset": 0},
                extractor=self.extractor_name,
            )
        ]


def make_store() -> PersistenceStore:
    generator = UUIDv7Generator(now_ms=lambda: 1_770_000_000_000, randbits=lambda bits: 42)
    return PersistenceStore.in_memory(id_factory=generator.new)


def test_fake_research_materializes_traceable_artifacts() -> None:
    store = make_store()
    result = SearchResult(
        result_id="source-1",
        title="Formal source",
        url="https://example.org/formal-source",
        provider="fixture",
        snippet="fixture snippet",
    )
    document = FetchedDocument(
        result_id="source-1",
        url=result.url,
        title=result.title,
        text="Formal text from a replayed non-video source.",
        fetched_at="2026-07-01T00:00:00Z",
        fetcher="replay-fetcher",
    )
    service = FormalResearchService(
        provider=FakeSearchProvider([result]),
        fetcher=ReplayFetcher({"source-1": document}),
        extractor=FixtureExtractor(),
        materializer=FormalResearchMaterializer(store),
    )

    run = service.run(ResearchQuery(topic_id="topic-1", query="formal research fixture"))

    assert run.plan_version_id
    assert run.artifact_version_id
    assert len(run.source_version_ids) == 1
    assert len(run.fetch_version_ids) == 1
    assert len(run.evidence_version_ids) == 1
    kinds = {
        row["object_kind"]: row["count"]
        for row in store.conn.execute("SELECT object_kind, count(*) AS count FROM trace_root GROUP BY object_kind")
    }
    assert kinds["research_plan"] == 1
    assert kinds["research_source"] == 1
    assert kinds["research_fetch"] == 1
    assert kinds["research_evidence"] == 1
    assert kinds["research_artifact"] == 1
    refs = store.conn.execute("SELECT relation_role FROM object_reference").fetchall()
    assert {row["relation_role"] for row in refs} >= {"found_source", "fetched_from", "extracted_from", "uses_evidence"}
    audits = store.conn.execute("SELECT event_type FROM audit_event").fetchall()
    assert [row["event_type"] for row in audits] == ["goal06.formal_research.materialized"]
    print("PASS fake formal research materializes traceable artifacts")


def test_video_sources_rejected_before_materialization() -> None:
    store = make_store()
    video_result = SearchResult(
        result_id="video-1",
        title="Video source",
        url="https://www.douyin.com/video/1",
        provider="fixture",
        source_type="video",
        platform="douyin",
    )
    document = FetchedDocument(
        result_id="video-1",
        url=video_result.url,
        title=video_result.title,
        text="not allowed",
        fetched_at="2026-07-01T00:00:00Z",
        fetcher="replay-fetcher",
        source_type="video",
        platform="douyin",
    )
    service = FormalResearchService(
        provider=FakeSearchProvider([video_result]),
        fetcher=ReplayFetcher({"video-1": document}),
        extractor=FixtureExtractor(),
        materializer=FormalResearchMaterializer(store),
    )

    try:
        service.run(ResearchQuery(topic_id="topic-2", query="blocked video fixture"))
    except ResearchBoundaryError:
        assert store.conn.execute("SELECT count(*) FROM trace_root").fetchone()[0] == 0
        print("PASS video platform source rejected before materialization")
        return
    raise AssertionError("expected video source rejection")


def test_repeated_runs_append_history_without_overwrite() -> None:
    store = make_store()
    result = SearchResult(
        result_id="source-1",
        title="Formal source",
        url="https://example.org/formal-source",
        provider="fixture",
    )
    document = FetchedDocument(
        result_id="source-1",
        url=result.url,
        title=result.title,
        text="Formal text from a replayed non-video source.",
        fetched_at="2026-07-01T00:00:00Z",
        fetcher="replay-fetcher",
    )
    service = FormalResearchService(
        provider=FakeSearchProvider([result]),
        fetcher=ReplayFetcher({"source-1": document}),
        extractor=FixtureExtractor(),
        materializer=FormalResearchMaterializer(store),
    )

    first = service.run(ResearchQuery(topic_id="topic-3", query="history fixture"))
    second = service.run(ResearchQuery(topic_id="topic-3", query="history fixture"))

    assert first.artifact_version_id != second.artifact_version_id
    assert store.conn.execute("SELECT count(*) FROM trace_version WHERE projection_version LIKE 'goal06.%'").fetchone()[0] == 10
    print("PASS repeated research runs append history without overwrite")


def test_topic_first_workflow_enqueue_and_dispatch() -> None:
    generator = UUIDv7Generator(now_ms=lambda: 1_770_000_000_000, randbits=lambda bits: 42)
    scheduler = Goal03Scheduler.in_memory(id_factory=generator.new, now_ms=lambda: 1_770_000_000_000)
    orchestrator = Goal05WorkflowOrchestrator(scheduler)
    host = RuntimeHost(scheduler, worker_id="goal06-research-worker")

    result = SearchResult(
        result_id="source-1",
        title="Workflow formal source",
        url="https://example.org/workflow-formal-source",
        provider="fixture",
    )
    document = FetchedDocument(
        result_id="source-1",
        url=result.url,
        title=result.title,
        text="Workflow replayed non-video source.",
        fetched_at="2026-07-01T00:00:00Z",
        fetcher="replay-fetcher",
    )
    service = FormalResearchService(
        provider=FakeSearchProvider([result]),
        fetcher=ReplayFetcher({"source-1": document}),
        extractor=FixtureExtractor(),
        materializer=FormalResearchMaterializer(scheduler.store),
    )
    host.register_handler(
        GOAL06_TOPIC_FIRST_JOB_KIND,
        make_formal_research_runtime_handler(service),
        contract=RuntimeHandlerContract(
            job_kind=GOAL06_TOPIC_FIRST_JOB_KIND,
            required_payload_keys=("topic_id", "query", "purpose"),
            required_result_keys=("plan_version_id", "artifact_version_id", "evidence_count"),
        ),
    )

    started = start_topic_first_research_workflow(
        orchestrator=orchestrator,
        topic_id="topic-4",
        query="workflow fixture",
        idempotency_key="goal06-topic-4",
    )
    replay = start_topic_first_research_workflow(
        orchestrator=orchestrator,
        topic_id="topic-4",
        query="workflow fixture",
        idempotency_key="goal06-topic-4",
    )
    assert started.workflow_id == replay.workflow_id
    assert started.job_ids == replay.job_ids
    assert replay.replayed

    runtime_result = host.run_once()
    assert runtime_result.status == "succeeded"
    job = scheduler.get_job(started.job_ids[0])
    assert job["status"] == "succeeded"
    assert scheduler.store.conn.execute(
        "SELECT count(*) FROM trace_root WHERE object_kind='research_artifact'"
    ).fetchone()[0] == 1
    print("PASS topic-first research workflow enqueue and runtime dispatch")


def main() -> None:
    test_fake_research_materializes_traceable_artifacts()
    test_video_sources_rejected_before_materialization()
    test_repeated_runs_append_history_without_overwrite()
    test_topic_first_workflow_enqueue_and_dispatch()
    print("GOAL-06 verification passed")


if __name__ == "__main__":
    main()
