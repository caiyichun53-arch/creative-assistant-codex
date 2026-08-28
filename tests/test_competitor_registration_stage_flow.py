from __future__ import annotations

import unittest
from types import SimpleNamespace
from unittest.mock import Mock, patch

from scripts.core.production.stage1_competitor_registration import (
    ConfiguredCompetitorRegistrationExecutor,
)


class _MemoryCore:
    def __init__(self) -> None:
        self.items: list[dict] = []

    def list_competitor_registration_items(self, *, registration_id: str, step_name: str):
        return [
            dict(item)
            for item in self.items
            if item["registration_id"] == registration_id
            and item["step_name"] == step_name
        ]

    def record_competitor_registration_item(
        self, *, registration_id: str, step_name: str, item_ref: str,
        status: str, artifact: dict | None, error: dict | None,
    ) -> None:
        self.items.append({
            "registration_id": registration_id,
            "step_name": step_name,
            "item_ref": item_ref,
            "status": status,
            "artifact": artifact,
            "error": error,
        })


class _Collector:
    def collect_video_snapshots(self, *, platform, source_urls, with_comments, max_comments_per_item):
        source_id = source_urls[0].rsplit("/", 1)[-1]
        return SimpleNamespace(
            raw_archive_ref="archive://prepared",
            payload={
                "items": [{
                    "source_id": source_id,
                    "music_download_url": "https://media.test/audio.wav",
                    "video_download_url": "",
                }],
                "comments": [],
            },
        )


class CompetitorRegistrationStageFlowTests(unittest.TestCase):
    def test_preparation_only_materializes_and_never_starts_breakdown(self) -> None:
        core = _MemoryCore()
        executor = object.__new__(ConfiguredCompetitorRegistrationExecutor)
        executor.core = core
        executor.collector = _Collector()
        executor.progress_callback = None
        executor.on_material_change = None

        registration = {"registration_id": "registration-1"}
        selected = [
            {
                "source_id": "source-1",
                "platform": "douyin",
                "url": "https://video.test/source-1",
                "metrics": {"like_count": 10},
            },
            {
                "source_id": "source-2",
                "platform": "douyin",
                "url": "https://video.test/source-2",
                "metrics": {"like_count": 20},
            },
        ]
        breakdown = Mock(side_effect=AssertionError("preparation must not invoke breakdown"))
        with patch.object(
            executor,
            "_materialize_collected_detail",
            side_effect=lambda *, item, detail_item, comments, collection_ref: {
                "artifact_kind": "transcript_and_comments",
                "source_id": item["source_id"],
                "transcript_ref": f"transcript://{item['source_id']}",
            },
        ), patch.object(executor, "process_prepared_breakdown", breakdown):
            artifacts = executor.prepare_selected_materials(
                registration=registration,
                selected_items=selected,
            )

        self.assertEqual([item["source_id"] for item in artifacts], ["source-1", "source-2"])
        self.assertEqual({item["step_name"] for item in core.items}, {"transcripts_and_comments"})
        breakdown.assert_not_called()


if __name__ == "__main__":
    unittest.main()
