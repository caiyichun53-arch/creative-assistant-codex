from __future__ import annotations

from dataclasses import dataclass
import hashlib
import json
from pathlib import Path
from typing import Any, Protocol

from scripts.core.research.goal06_formal_research import FetchedDocument, ResearchQuery, SearchResult


class ExternalAdapterError(RuntimeError):
    pass


def _stable_hash(value: Any) -> str:
    payload = json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode("utf-8")
    return hashlib.sha256(payload).hexdigest()


def _require_text(value: Any, field: str) -> str:
    text = str(value or "").strip()
    if not text:
        raise ExternalAdapterError(f"{field} is required")
    return text


def _require_positive_int(value: Any, field: str, *, maximum: int) -> int:
    try:
        parsed = int(value)
    except (TypeError, ValueError) as exc:
        raise ExternalAdapterError(f"{field} must be an integer") from exc
    if parsed < 1:
        raise ExternalAdapterError(f"{field} must be positive")
    if parsed > maximum:
        raise ExternalAdapterError(f"{field} exceeds controlled validation maximum")
    return parsed


@dataclass(frozen=True)
class ExternalAdapterCommand:
    adapter_id: str
    capability: str
    executable: str
    args: tuple[str, ...]
    input_payload: dict[str, Any]
    env_keys: tuple[str, ...] = ()
    max_items: int = 1
    timeout_seconds: int = 60

    def sanitized_manifest(self) -> dict[str, Any]:
        return {
            "adapter_id": self.adapter_id,
            "capability": self.capability,
            "executable": self.executable,
            "args": list(self.args),
            "input_hash": _stable_hash(self.input_payload),
            "env_keys": list(self.env_keys),
            "max_items": self.max_items,
            "timeout_seconds": self.timeout_seconds,
        }


@dataclass(frozen=True)
class ExternalCommandResult:
    status: str
    payload: dict[str, Any]
    raw_archive_ref: str | None = None
    external_side_effect: bool = True


class ExternalCommandExecutor(Protocol):
    def execute(self, command: ExternalAdapterCommand) -> ExternalCommandResult:
        ...


@dataclass(frozen=True)
class ExternalAdapterRunResult:
    adapter_id: str
    capability: str
    item_count: int
    payload: dict[str, Any]
    command_hash: str
    output_hash: str
    raw_archive_ref: str | None
    external_side_effect: bool

    def as_runtime_result(self) -> dict[str, Any]:
        return {
            "adapter_id": self.adapter_id,
            "capability": self.capability,
            "item_count": self.item_count,
            "command_hash": self.command_hash,
            "output_hash": self.output_hash,
            "raw_archive_ref": self.raw_archive_ref,
            "external_side_effect": self.external_side_effect,
        }


class _ExternalAdapterBase:
    adapter_id = "external.base"

    def __init__(self, executor: ExternalCommandExecutor):
        self.executor = executor

    def _execute(self, command: ExternalAdapterCommand, *, expected_capability: str) -> ExternalCommandResult:
        if command.adapter_id != self.adapter_id:
            raise ExternalAdapterError("adapter command id mismatch")
        if command.capability != expected_capability:
            raise ExternalAdapterError("adapter command capability mismatch")
        result = self.executor.execute(command)
        if result.status != "succeeded":
            detail = str(result.payload.get("error") or "").strip() if isinstance(result.payload, dict) else ""
            suffix = f": {detail}" if detail else ""
            raise ExternalAdapterError(f"{self.adapter_id} command failed: {result.status}{suffix}")
        if not isinstance(result.payload, dict):
            raise ExternalAdapterError("adapter command payload must be an object")
        return result

    def _run_result(self, command: ExternalAdapterCommand, result: ExternalCommandResult, payload: dict[str, Any], item_count: int) -> ExternalAdapterRunResult:
        return ExternalAdapterRunResult(
            adapter_id=self.adapter_id,
            capability=command.capability,
            item_count=item_count,
            payload=payload,
            command_hash=_stable_hash(command.sanitized_manifest()),
            output_hash=_stable_hash(payload),
            raw_archive_ref=result.raw_archive_ref,
            external_side_effect=result.external_side_effect,
        )


class MediaCrawlerCollectorAdapter(_ExternalAdapterBase):
    adapter_id = "collector.mediacrawler"
    capability = "platform.video_snapshot"
    supported_platforms = frozenset({"douyin", "kuaishou", "bilibili", "xiaohongshu", "weibo", "tieba", "zhihu"})

    def collect_video_snapshot(
        self,
        *,
        platform: str,
        source_url: str,
        max_items: int = 1,
        with_comments: bool = False,
    ) -> ExternalAdapterRunResult:
        platform_name = _require_text(platform, "platform").lower()
        if platform_name not in self.supported_platforms:
            raise ExternalAdapterError(f"unsupported MediaCrawler platform: {platform}")
        cap = _require_positive_int(max_items, "max_items", maximum=500)
        url = _require_text(source_url, "source_url")
        source_kind = "creator" if "/user/" in url or (platform_name == "douyin" and url.startswith("MS4wLjABAAAA")) else "detail"
        command = ExternalAdapterCommand(
            adapter_id=self.adapter_id,
            capability=self.capability,
            executable="vendor/MediaCrawler/main.py",
            args=(platform_name, source_kind, "--get_comment", "yes" if with_comments else "no"),
            input_payload={
                "platform": platform_name,
                "source_url": url,
                "source_kind": source_kind,
                "with_comments": with_comments,
            },
            env_keys=("COLLECTOR_TEST_PROFILE_DIR",),
            max_items=cap,
            timeout_seconds=120,
        )
        result = self._execute(command, expected_capability=self.capability)
        items = result.payload.get("items")
        if not isinstance(items, list):
            raise ExternalAdapterError("MediaCrawler result must contain items")
        normalized: list[dict[str, Any]] = []
        seen_source_ids: set[str] = set()
        for raw_item in items:
            item = self._normalize_video_item(raw_item, platform_name)
            source_id = item["source_id"]
            if source_id in seen_source_ids:
                continue
            seen_source_ids.add(source_id)
            normalized.append(item)
            if len(normalized) >= cap:
                break
        comments = result.payload.get("comments", [])
        if with_comments and not isinstance(comments, list):
            raise ExternalAdapterError("MediaCrawler result must contain a comments array when comments are requested")
        payload = {
            "items": normalized,
            "source_platform": platform_name,
            "comments_requested": with_comments,
            "comments": comments if isinstance(comments, list) else [],
        }
        return self._run_result(command, result, payload, len(normalized))

    def collect_video_snapshots(
        self,
        *,
        platform: str,
        source_urls: tuple[str, ...],
        with_comments: bool = False,
        max_comments_per_item: int = 60,
    ) -> ExternalAdapterRunResult:
        platform_name = _require_text(platform, "platform").lower()
        if platform_name not in self.supported_platforms:
            raise ExternalAdapterError(f"unsupported MediaCrawler platform: {platform}")
        urls = tuple(dict.fromkeys(
            _require_text(value, "source_url") for value in source_urls
        ))
        if not urls or len(urls) > 20:
            raise ExternalAdapterError("MediaCrawler detail batches require between 1 and 20 videos")
        comment_limit = _require_positive_int(
            max_comments_per_item,
            "max_comments_per_item",
            maximum=100,
        )
        command = ExternalAdapterCommand(
            adapter_id=self.adapter_id,
            capability=self.capability,
            executable="vendor/MediaCrawler/main.py",
            args=(platform_name, "detail", "--get_comment", "yes" if with_comments else "no"),
            input_payload={
                "platform": platform_name,
                "source_urls": list(urls),
                "source_kind": "detail",
                "with_comments": with_comments,
                "max_comments_per_item": comment_limit,
            },
            env_keys=("COLLECTOR_TEST_PROFILE_DIR",),
            max_items=len(urls),
            timeout_seconds=max(120, len(urls) * 45),
        )
        result = self._execute(command, expected_capability=self.capability)
        items = result.payload.get("items")
        if not isinstance(items, list):
            raise ExternalAdapterError("MediaCrawler batch result must contain items")
        normalized: list[dict[str, Any]] = []
        seen_source_ids: set[str] = set()
        for raw_item in items:
            item = self._normalize_video_item(raw_item, platform_name)
            source_id = item["source_id"]
            if source_id in seen_source_ids:
                continue
            seen_source_ids.add(source_id)
            normalized.append(item)
        comments = result.payload.get("comments", [])
        if with_comments and not isinstance(comments, list):
            raise ExternalAdapterError("MediaCrawler batch result must contain a comments array")
        payload = {
            "items": normalized,
            "source_platform": platform_name,
            "comments_requested": with_comments,
            "comments": comments if isinstance(comments, list) else [],
        }
        return self._run_result(command, result, payload, len(normalized))

    @staticmethod
    def _normalize_video_item(item: Any, platform: str) -> dict[str, Any]:
        if not isinstance(item, dict):
            raise ExternalAdapterError("MediaCrawler item must be an object")
        source_id = _require_text(item.get("source_id") or item.get("aweme_id") or item.get("id"), "source_id")
        url = _require_text(item.get("url") or item.get("source_url") or item.get("aweme_url"), "url")
        return {
            "source_id": source_id,
            "platform": platform,
            "source_type": "video_snapshot",
            "url": url,
            "title": str(item.get("title") or item.get("desc") or ""),
            "author": str(item.get("author") or item.get("nickname") or ""),
            "published_at": item.get("published_at") or item.get("create_time"),
            "duration_seconds": int(item.get("duration_seconds") or item.get("duration_sec") or 0),
            "music_download_url": str(item.get("music_download_url") or ""),
            "video_download_url": str(item.get("video_download_url") or item.get("video_url") or ""),
            "metrics": {
                "like_count": int(item.get("like_count") or item.get("liked_count") or 0),
                "comment_count": int(item.get("comment_count") or 0),
                "share_count": int(item.get("share_count") or 0),
                "collect_count": int(item.get("collect_count") or item.get("collected_count") or 0),
            },
        }


class CommentCollectionAdapter(_ExternalAdapterBase):
    adapter_id = "collector.comments"
    capability = "platform.comment_collection"

    def collect_comments(
        self,
        *,
        platform: str,
        source_id: str,
        source_url: str,
        max_comments: int = 60,
    ) -> ExternalAdapterRunResult:
        cap = _require_positive_int(max_comments, "max_comments", maximum=80)
        platform_name = _require_text(platform, "platform").lower()
        command = ExternalAdapterCommand(
            adapter_id=self.adapter_id,
            capability=self.capability,
            executable="vendor/MediaCrawler/main.py",
            args=(platform_name, "detail", "--get_comment", "yes"),
            input_payload={
                "platform": platform_name,
                "source_id": _require_text(source_id, "source_id"),
                "source_url": _require_text(source_url, "source_url"),
            },
            env_keys=("COLLECTOR_TEST_PROFILE_DIR",),
            max_items=cap,
            timeout_seconds=120,
        )
        result = self._execute(command, expected_capability=self.capability)
        comments = result.payload.get("comments")
        if not isinstance(comments, list):
            raise ExternalAdapterError("comment adapter result must contain comments")
        normalized = [self._normalize_comment(item, platform_name, source_id) for item in comments[:cap]]
        payload = {"comments": normalized, "source_platform": platform_name, "source_id": source_id}
        return self._run_result(command, result, payload, len(normalized))

    @staticmethod
    def _normalize_comment(item: Any, platform: str, source_id: str) -> dict[str, Any]:
        if not isinstance(item, dict):
            raise ExternalAdapterError("comment item must be an object")
        return {
            "comment_id": _require_text(item.get("comment_id") or item.get("id"), "comment_id"),
            "source_id": source_id,
            "platform": platform,
            "source_type": "short_comment",
            "text": _require_text(item.get("text") or item.get("content"), "text"),
            "like_count": int(item.get("like_count") or 0),
            "parent_comment_id": item.get("parent_comment_id"),
        }


class AsrAdapter(_ExternalAdapterBase):
    adapter_id = "asr.sensevoice"
    capability = "media.transcription"

    def transcribe(
        self,
        *,
        media_ref: str,
        media_path: str,
        language: str = "zh",
        max_duration_seconds: int = 180,
    ) -> ExternalAdapterRunResult:
        duration = _require_positive_int(max_duration_seconds, "max_duration_seconds", maximum=600)
        command = ExternalAdapterCommand(
            adapter_id=self.adapter_id,
            capability=self.capability,
            executable="adapter.asr.sensevoice",
            args=("--media", "<controlled-media-path>", "--language", language),
            input_payload={
                "media_ref": _require_text(media_ref, "media_ref"),
                "controlled_media_path": _require_text(media_path, "media_path"),
                "media_path_hash": _stable_hash(_require_text(media_path, "media_path")),
                "language": language,
                "max_duration_seconds": duration,
            },
            env_keys=("ASR_TEST_MEDIA_PATH", "ASR_MODELS_ROOT"),
            max_items=1,
            timeout_seconds=300,
        )
        result = self._execute(command, expected_capability=self.capability)
        if "transcript_text" in result.payload:
            raise ExternalAdapterError("ASR adapter evidence must not expose full transcript_text")
        transcript_ref = _require_text(result.payload.get("transcript_ref"), "transcript_ref")
        transcript_hash = _require_text(result.payload.get("transcript_hash"), "transcript_hash")
        payload = {
            "media_ref": media_ref,
            "transcript_ref": transcript_ref,
            "transcript_hash": transcript_hash,
            "asr_model_ref": str(result.payload.get("asr_model_ref") or self.adapter_id),
            "vad_model_ref": str(result.payload.get("vad_model_ref") or "not_reported"),
            "duration_seconds": int(result.payload.get("duration_seconds") or 0),
            "segment_count": int(result.payload.get("segment_count") or 0),
            "quality_status": str(result.payload.get("quality_status") or "unknown"),
        }
        return self._run_result(command, result, payload, 1)


class NetEaseMusicCollectorAdapter(_ExternalAdapterBase):
    adapter_id = "collector.netease_music"
    capability = "music.comment_collection"

    def collect_song_comments(self, *, song_id: str, max_comments: int = 100) -> ExternalAdapterRunResult:
        cap = _require_positive_int(max_comments, "max_comments", maximum=100)
        command = ExternalAdapterCommand(
            adapter_id=self.adapter_id,
            capability=self.capability,
            executable="adapter.netease_music.comments",
            args=("comments", "--song-id", "<controlled-song-id>"),
            input_payload={"song_id": _require_text(song_id, "song_id")},
            max_items=cap,
            timeout_seconds=120,
        )
        result = self._execute(command, expected_capability=self.capability)
        comments = result.payload.get("comments")
        if not isinstance(comments, list):
            raise ExternalAdapterError("NetEase adapter result must contain comments")
        normalized = [self._normalize_comment(item, song_id) for item in comments[:cap]]
        payload = {"song_id": song_id, "platform": "netease_music", "comments": normalized}
        return self._run_result(command, result, payload, len(normalized))

    @staticmethod
    def _normalize_comment(item: Any, song_id: str) -> dict[str, Any]:
        if not isinstance(item, dict):
            raise ExternalAdapterError("NetEase comment must be an object")
        return {
            "comment_id": _require_text(item.get("comment_id") or item.get("id"), "comment_id"),
            "song_id": song_id,
            "platform": "netease_music",
            "source_type": "short_comment",
            "text": _require_text(item.get("text") or item.get("content"), "text"),
            "like_count": int(item.get("like_count") or 0),
        }


class SearchProviderAdapter(_ExternalAdapterBase):
    adapter_id = "search.provider"
    capability = "research.search"
    provider_name = "external-search-provider"

    def search(self, query: ResearchQuery) -> list[SearchResult]:
        command = ExternalAdapterCommand(
            adapter_id=self.adapter_id,
            capability=self.capability,
            executable="external-search-provider",
            args=("search",),
            input_payload={"topic_id": query.topic_id, "query": query.query, "purpose": query.purpose},
            env_keys=("SEARCH_PROVIDER_API_KEY", "SEARCH_PROVIDER_NAME"),
            max_items=5,
            timeout_seconds=60,
        )
        result = self._execute(command, expected_capability=self.capability)
        rows = result.payload.get("results")
        if not isinstance(rows, list):
            raise ExternalAdapterError("search adapter result must contain results")
        return [self._to_search_result(row) for row in rows]

    def _to_search_result(self, row: Any) -> SearchResult:
        if not isinstance(row, dict):
            raise ExternalAdapterError("search result must be an object")
        return SearchResult(
            result_id=_require_text(row.get("result_id") or row.get("id"), "result_id"),
            title=_require_text(row.get("title"), "title"),
            url=_require_text(row.get("url"), "url"),
            provider=str(row.get("provider") or self.provider_name),
            source_type=str(row.get("source_type") or "web_page"),
            platform=row.get("platform"),
            snippet=str(row.get("snippet") or ""),
            metadata=dict(row.get("metadata") or {}),
        )


class ResearchFetcherAdapter(_ExternalAdapterBase):
    adapter_id = "research.fetcher"
    capability = "research.fetch"
    fetcher_name = "external-research-fetcher"

    def fetch(self, result: SearchResult) -> FetchedDocument:
        command = ExternalAdapterCommand(
            adapter_id=self.adapter_id,
            capability=self.capability,
            executable="external-research-fetcher",
            args=("fetch",),
            input_payload={"result_id": result.result_id, "url": result.url, "source_type": result.source_type},
            env_keys=("SEARCH_PROVIDER_API_KEY",),
            max_items=1,
            timeout_seconds=60,
        )
        command_result = self._execute(command, expected_capability=self.capability)
        document = command_result.payload.get("document")
        if not isinstance(document, dict):
            raise ExternalAdapterError("fetch adapter result must contain document")
        return FetchedDocument(
            result_id=result.result_id,
            url=result.url,
            title=str(document.get("title") or result.title),
            text=_require_text(document.get("text"), "document.text"),
            fetched_at=_require_text(document.get("fetched_at"), "document.fetched_at"),
            fetcher=str(document.get("fetcher") or self.fetcher_name),
            source_type=str(document.get("source_type") or result.source_type),
            platform=document.get("platform") or result.platform,
            metadata=dict(document.get("metadata") or {}),
        )


def _payload_text(payload: dict[str, Any], key: str) -> str:
    return _require_text(payload.get(key), key)


def make_mediacrawler_runtime_handler(adapter: MediaCrawlerCollectorAdapter):
    def handle(payload: dict[str, Any]) -> dict[str, Any]:
        result = adapter.collect_video_snapshot(
            platform=_payload_text(payload, "platform"),
            source_url=_payload_text(payload, "source_url"),
            max_items=int(payload.get("max_items") or 1),
            with_comments=bool(payload.get("with_comments", False)),
        )
        return result.as_runtime_result()

    return handle


def make_comment_collection_runtime_handler(adapter: CommentCollectionAdapter):
    def handle(payload: dict[str, Any]) -> dict[str, Any]:
        result = adapter.collect_comments(
            platform=_payload_text(payload, "platform"),
            source_id=_payload_text(payload, "source_id"),
            source_url=_payload_text(payload, "source_url"),
            max_comments=int(payload.get("max_comments") or 60),
        )
        return result.as_runtime_result()

    return handle


def make_asr_runtime_handler(adapter: AsrAdapter):
    def handle(payload: dict[str, Any]) -> dict[str, Any]:
        result = adapter.transcribe(
            media_ref=_payload_text(payload, "media_ref"),
            media_path=_payload_text(payload, "media_path"),
            language=str(payload.get("language") or "zh"),
            max_duration_seconds=int(payload.get("max_duration_seconds") or 180),
        )
        return result.as_runtime_result()

    return handle


def make_netease_runtime_handler(adapter: NetEaseMusicCollectorAdapter):
    def handle(payload: dict[str, Any]) -> dict[str, Any]:
        result = adapter.collect_song_comments(
            song_id=_payload_text(payload, "song_id"),
            max_comments=int(payload.get("max_comments") or 100),
        )
        return result.as_runtime_result()

    return handle


def local_repo_path(*parts: str) -> Path:
    return Path(__file__).resolve().parents[3].joinpath(*parts)
