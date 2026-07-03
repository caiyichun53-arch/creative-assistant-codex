from __future__ import annotations

import unittest

from scripts.core.external_adapters import (
    AsrAdapter,
    CommentCollectionAdapter,
    ExternalAdapterCommand,
    ExternalAdapterError,
    ExternalCommandResult,
    MediaCrawlerCollectorAdapter,
    NetEaseMusicCollectorAdapter,
    ResearchFetcherAdapter,
    SearchProviderAdapter,
    make_comment_collection_runtime_handler,
)
from scripts.core.persistence.goal01_store import PersistenceStore, UUIDv7Generator
from scripts.core.research.goal06_formal_research import (
    ExtractedEvidence,
    FormalResearchMaterializer,
    FormalResearchService,
    ResearchQuery,
)
from scripts.core.runtime.goal04_runtime_host import RuntimeAdapter, RuntimeHandlerContract, RuntimeHost, RuntimeHostError
from scripts.core.scheduler.goal03_scheduler import Goal03Scheduler


class RecordingExecutor:
    def __init__(self, fixtures: dict[str, dict], *, error: Exception | None = None):
        self.fixtures = fixtures
        self.error = error
        self.commands: list[ExternalAdapterCommand] = []

    def execute(self, command: ExternalAdapterCommand) -> ExternalCommandResult:
        self.commands.append(command)
        if self.error is not None:
            raise self.error
        payload = self.fixtures.get(command.capability)
        if payload is None:
            return ExternalCommandResult(status="failed", payload={"error": "missing fixture"})
        return ExternalCommandResult(
            status="succeeded",
            payload=payload,
            raw_archive_ref=f"fixture://{command.capability}",
            external_side_effect=True,
        )


class FixtureExtractor:
    extractor_name = "phase4-fixture-extractor"

    def extract(self, document):
        return [
            ExtractedEvidence(
                evidence_id=f"ev-{document.result_id}",
                result_id=document.result_id,
                claim="fixture document supports a traceable claim",
                quote=document.text[:60],
                locator={"url": document.url, "offset": 0},
                extractor=self.extractor_name,
            )
        ]


def make_scheduler() -> Goal03Scheduler:
    generator = UUIDv7Generator(now_ms=lambda: 1_770_000_000_000, randbits=lambda bits: 42)
    return Goal03Scheduler.in_memory(id_factory=generator.new, now_ms=lambda: 1_770_000_000_000)


class ExternalExecutorAdapterTests(unittest.TestCase):
    def test_mediacrawler_adapter_normalizes_video_snapshot_without_state_write(self) -> None:
        executor = RecordingExecutor(
            {
                "platform.video_snapshot": {
                    "items": [
                        {
                            "aweme_id": "aweme-1",
                            "url": "https://example.invalid/video/1",
                            "desc": "fixture video",
                            "like_count": 12,
                            "comment_count": 3,
                            "share_count": 2,
                        }
                    ]
                }
            }
        )

        result = MediaCrawlerCollectorAdapter(executor).collect_video_snapshot(
            platform="douyin",
            source_url="https://example.invalid/video/1",
            max_items=1,
            with_comments=True,
        )

        self.assertEqual(result.item_count, 1)
        self.assertEqual(result.payload["items"][0]["source_type"], "video_snapshot")
        self.assertEqual(result.payload["items"][0]["metrics"]["comment_count"], 3)
        self.assertEqual(len(executor.commands), 1)
        self.assertEqual(executor.commands[0].env_keys, ("COLLECTOR_TEST_PROFILE_DIR",))

    def test_comment_collection_adapter_is_separate_from_video_snapshot_adapter(self) -> None:
        executor = RecordingExecutor(
            {
                "platform.comment_collection": {
                    "comments": [
                        {"comment_id": "c1", "text": "这句评论保留真实口语感", "like_count": 9},
                        {"comment_id": "c2", "text": "第二条评论", "like_count": 3},
                    ]
                }
            }
        )

        result = CommentCollectionAdapter(executor).collect_comments(
            platform="douyin",
            source_id="aweme-1",
            source_url="https://example.invalid/video/1",
            max_comments=2,
        )

        self.assertEqual(result.item_count, 2)
        self.assertEqual(result.payload["comments"][0]["source_type"], "short_comment")
        self.assertEqual(executor.commands[0].capability, "platform.comment_collection")

    def test_asr_adapter_records_hash_and_ref_without_full_transcript(self) -> None:
        executor = RecordingExecutor(
            {
                "media.transcription": {
                    "transcript_ref": "artifact://transcripts/hit-1.txt",
                    "transcript_hash": "sha256:abc123",
                    "duration_seconds": 32,
                    "segment_count": 6,
                    "quality_status": "passed",
                }
            }
        )

        result = AsrAdapter(executor).transcribe(media_ref="hit-1", media_path="I:/tmp/hit-1.mp4")

        self.assertEqual(result.payload["transcript_hash"], "sha256:abc123")
        self.assertNotIn("transcript_text", result.payload)
        self.assertEqual(executor.commands[0].env_keys, ("ASR_TEST_MEDIA_PATH", "ASR_MODELS_ROOT"))

    def test_asr_adapter_rejects_full_transcript_in_evidence_payload(self) -> None:
        executor = RecordingExecutor(
            {
                "media.transcription": {
                    "transcript_ref": "artifact://transcripts/hit-1.txt",
                    "transcript_hash": "sha256:abc123",
                    "transcript_text": "full transcript must stay out of tracked evidence",
                }
            }
        )

        with self.assertRaises(ExternalAdapterError):
            AsrAdapter(executor).transcribe(media_ref="hit-1", media_path="I:/tmp/hit-1.mp4")

    def test_search_provider_and_fetcher_plug_into_formal_research_service(self) -> None:
        executor = RecordingExecutor(
            {
                "research.search": {
                    "results": [
                        {
                            "result_id": "source-1",
                            "title": "Formal web source",
                            "url": "https://example.org/formal",
                            "provider": "fixture-search",
                            "snippet": "fixture snippet",
                        }
                    ]
                },
                "research.fetch": {
                    "document": {
                        "title": "Formal web source",
                        "text": "Formal fetched text with traceable evidence.",
                        "fetched_at": "2026-07-03T00:00:00Z",
                    }
                },
            }
        )
        store = PersistenceStore.in_memory()
        self.addCleanup(store.conn.close)
        service = FormalResearchService(
            provider=SearchProviderAdapter(executor),
            fetcher=ResearchFetcherAdapter(executor),
            extractor=FixtureExtractor(),
            materializer=FormalResearchMaterializer(store),
        )

        run = service.run(ResearchQuery(topic_id="topic-1", query="formal fixture"))

        self.assertEqual(len(run.source_version_ids), 1)
        self.assertEqual(len(run.fetch_version_ids), 1)
        self.assertEqual(len(run.evidence_version_ids), 1)
        self.assertEqual([command.capability for command in executor.commands], ["research.search", "research.fetch"])

    def test_netease_music_adapter_normalizes_song_comments(self) -> None:
        executor = RecordingExecutor(
            {
                "music.comment_collection": {
                    "comments": [
                        {"comment_id": "nc1", "text": "听到这首歌会想起现场", "like_count": 22},
                    ]
                }
            }
        )

        result = NetEaseMusicCollectorAdapter(executor).collect_song_comments(song_id="12345", max_comments=1)

        self.assertEqual(result.item_count, 1)
        self.assertEqual(result.payload["comments"][0]["platform"], "netease_music")
        self.assertEqual(executor.commands[0].executable, "adapter.netease_music.comments")

    def test_runtime_host_rejects_external_adapter_by_default_and_allows_phase4_opt_in(self) -> None:
        executor = RecordingExecutor(
            {
                "platform.comment_collection": {
                    "comments": [{"comment_id": "c1", "text": "fixture comment"}],
                }
            }
        )
        adapter = RuntimeAdapter(
            adapter_name="comment-collector",
            job_kind="adapter.comments.collect",
            handler=make_comment_collection_runtime_handler(CommentCollectionAdapter(executor)),
            contract=RuntimeHandlerContract(
                job_kind="adapter.comments.collect",
                required_payload_keys=("platform", "source_id", "source_url"),
                required_result_keys=("adapter_id", "capability", "item_count", "output_hash"),
            ),
            uses_external_io=True,
        )
        scheduler = make_scheduler()
        self.addCleanup(scheduler.store.conn.close)
        with self.assertRaises(RuntimeHostError):
            RuntimeHost(scheduler, worker_id="default-worker").register_adapter(adapter)

        host = RuntimeHost(scheduler, worker_id="phase4-worker", allow_external_adapters=True)
        host.register_adapter(adapter)
        queued = scheduler.enqueue_job(
            job_kind="adapter.comments.collect",
            payload={"platform": "douyin", "source_id": "aweme-1", "source_url": "https://example.invalid/video/1"},
            idempotency_key="phase4-comment-collect",
            max_attempts=1,
        )
        step = host.run_once()
        self.assertEqual(step.status, "succeeded")
        self.assertEqual(scheduler.get_job(queued.job_id)["status"], "succeeded")
        self.assertEqual(len(executor.commands), 1)

    def test_external_adapter_failure_does_not_fallback_or_complete_runtime_job(self) -> None:
        executor = RecordingExecutor({}, error=RuntimeError("collector unavailable"))
        scheduler = make_scheduler()
        self.addCleanup(scheduler.store.conn.close)
        host = RuntimeHost(scheduler, worker_id="phase4-worker", allow_external_adapters=True)
        host.register_adapter(
            RuntimeAdapter(
                adapter_name="comment-collector",
                job_kind="adapter.comments.collect",
                handler=make_comment_collection_runtime_handler(CommentCollectionAdapter(executor)),
                contract=RuntimeHandlerContract(
                    job_kind="adapter.comments.collect",
                    required_payload_keys=("platform", "source_id", "source_url"),
                    required_result_keys=("adapter_id", "capability", "item_count", "output_hash"),
                ),
                uses_external_io=True,
            )
        )
        queued = scheduler.enqueue_job(
            job_kind="adapter.comments.collect",
            payload={"platform": "douyin", "source_id": "aweme-1", "source_url": "https://example.invalid/video/1"},
            idempotency_key="phase4-comment-failure",
            max_attempts=1,
        )

        step = host.run_once()

        self.assertEqual(step.status, "failed")
        self.assertEqual(step.reason, "handler_error")
        self.assertEqual(scheduler.get_job(queued.job_id)["status"], "dead")
        self.assertEqual(len(executor.commands), 1)


if __name__ == "__main__":
    unittest.main()
