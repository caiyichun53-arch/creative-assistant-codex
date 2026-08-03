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
from scripts.core.external_adapters.local_mediacrawler_executor import LocalMediaCrawlerExecutor
from scripts.core.external_adapters.local_sensevoice_executor import LocalSenseVoiceExecutor
from scripts.core.external_adapters.local_trendradar_executor import LocalTrendRadarExecutor
from scripts.core.external_adapters.competitor_registration_runtime import (
    LocalCompetitorMediaMaterializer,
    MaterializedAudio,
)
from scripts.core.external_adapters.music_audience_browser import LocalMusicAudienceBrowserExecutor
from scripts.core.external_adapters.local_voxcpm2_executor import (
    LocalVoxCPM2AudioExecutor,
    VoxCPM2ExecutionError,
    VoxCPM2SynthesisRequest,
    VoxCPM2SynthesisResult,
)

__all__ = [
    "AsrAdapter",
    "CommentCollectionAdapter",
    "ExternalAdapterCommand",
    "ExternalAdapterError",
    "ExternalAdapterRunResult",
    "ExternalCommandExecutor",
    "ExternalCommandResult",
    "LocalMediaCrawlerExecutor",
    "LocalSenseVoiceExecutor",
    "LocalTrendRadarExecutor",
    "LocalCompetitorMediaMaterializer",
    "MaterializedAudio",
    "LocalMusicAudienceBrowserExecutor",
    "LocalVoxCPM2AudioExecutor",
    "VoxCPM2ExecutionError",
    "VoxCPM2SynthesisRequest",
    "VoxCPM2SynthesisResult",
    "MediaCrawlerCollectorAdapter",
    "NetEaseMusicCollectorAdapter",
    "ResearchFetcherAdapter",
    "SearchProviderAdapter",
    "make_asr_runtime_handler",
    "make_comment_collection_runtime_handler",
    "make_mediacrawler_runtime_handler",
    "make_netease_runtime_handler",
]
