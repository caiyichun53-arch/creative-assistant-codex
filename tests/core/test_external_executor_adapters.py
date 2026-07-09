from __future__ import annotations

# 2026-07-11: this file used to also cover RuntimeAdapter/RuntimeHost from
# scripts/core/runtime/goal04_runtime_host.py (two tests:
# test_runtime_host_rejects_external_adapter_by_default_and_allows_phase4_opt_in,
# test_external_adapter_failure_does_not_fallback_or_complete_runtime_job).
# scripts/core/runtime/ was archived to archive/dead_goal_chain_20260709/ after
# a real production-entrypoint import closure proved it has zero real caller
# (see that archive's README). Those two tests moved with it -- they tested
# RuntimeHost's own adapter-registration/failure semantics, not the adapter
# classes below. The six tests that remain are unaffected: they test the
# adapter classes themselves (MediaCrawlerCollectorAdapter/CommentCollection
# Adapter/AsrAdapter/SearchProviderAdapter/NetEaseMusicCollectorAdapter,
# defined in the real, still-imported scripts/core/external_adapters/
# goal_phase4_external_adapters.py) against a fake ExternalAdapterCommand
# executor, with no dependency on the archived runtime host.

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
)
from scripts.core.persistence.goal01_store import PersistenceStore
from scripts.core.research.goal06_formal_research import (
    ExtractedEvidence,
    FormalResearchMaterializer,
    FormalResearchService,
    ResearchQuery,
)


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


if __name__ == "__main__":
    unittest.main()
