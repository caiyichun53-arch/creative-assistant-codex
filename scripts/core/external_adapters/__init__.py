from __future__ import annotations

from scripts.core.external_adapters.goal_phase4_external_adapters import (
    AsrAdapter,
    CommentCollectionAdapter,
    ExternalAdapterCommand,
    ExternalAdapterError,
    ExternalAdapterRunResult,
    ExternalCommandExecutor,
    ExternalCommandResult,
    MediaCrawlerCollectorAdapter,
    NetEaseMusicCollectorAdapter,
    ResearchFetcherAdapter,
    SearchProviderAdapter,
    make_asr_runtime_handler,
    make_comment_collection_runtime_handler,
    make_mediacrawler_runtime_handler,
    make_netease_runtime_handler,
)

__all__ = [
    "AsrAdapter",
    "CommentCollectionAdapter",
    "ExternalAdapterCommand",
    "ExternalAdapterError",
    "ExternalAdapterRunResult",
    "ExternalCommandExecutor",
    "ExternalCommandResult",
    "MediaCrawlerCollectorAdapter",
    "NetEaseMusicCollectorAdapter",
    "ResearchFetcherAdapter",
    "SearchProviderAdapter",
    "make_asr_runtime_handler",
    "make_comment_collection_runtime_handler",
    "make_mediacrawler_runtime_handler",
    "make_netease_runtime_handler",
]
