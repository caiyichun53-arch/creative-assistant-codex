"""Daily source acquisition: TrendRadar first, then three one-page tag searches."""

from __future__ import annotations

import hashlib
import json
import sqlite3
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from scripts.core.business_data.run_domain_search import run_daily_tag_searches
from scripts.core.execution_contract import require_baseline_citations
from scripts.core.external_adapters import ExternalAdapterCommand, ExternalCommandExecutor
from scripts.integrations.trendradar_runtime import read_original_article


TRENDRADAR_INSTALL_DIR = Path(__file__).resolve().parents[3] / "vendor" / "TrendRadar"


def _canonical(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def _stable_id(value: Any) -> str:
    return hashlib.sha256(_canonical(value).encode("utf-8")).hexdigest()[:20]


def validate_hotspot_collection_contract(config: dict[str, Any]) -> dict[str, Any]:
    contract = require_baseline_citations(["3", "16"])
    if str(config.get("provider") or "").strip().lower() != "trendradar":
        raise ValueError("daily hotspot collection provider must be TrendRadar")
    if not bool(config.get("live_enabled", False)):
        raise ValueError("hotspot_collection.live_enabled is false; real TrendRadar collection is blocked")
    return contract


def collect_trendradar_hotspots(
    conn: sqlite3.Connection,
    executor: ExternalCommandExecutor,
    *,
    discovery_run_id: str,
    config: dict[str, Any],
    now: datetime | None = None,
) -> dict[str, Any]:
    """Collect once, persist every valid normalized item, and never retry."""
    validate_hotspot_collection_contract(config)
    now = now or datetime.now(timezone.utc)
    command = ExternalAdapterCommand(
        adapter_id="collector.trendradar",
        capability="hotspot.daily_snapshot",
        executable=str(config.get("executable") or "trendradar"),
        args=tuple(str(value) for value in config.get("args", [])),
        input_payload={"provider": "trendradar", "discovery_run_id": discovery_run_id},
        max_items=max(int(config.get("max_items", 500)), 1),
        timeout_seconds=max(int(config.get("timeout_seconds", 180)), 1),
    )
    command_hash = hashlib.sha256(_canonical(command.sanitized_manifest()).encode("utf-8")).hexdigest()
    collection_run_id = f"trendradar_run_{_stable_id({'run': discovery_run_id, 'command': command_hash})}"
    conn.execute(
        """
        INSERT INTO trendradar_collection_run(
            collection_run_id, discovery_run_id, status, command_hash, started_at
        ) VALUES (?, ?, 'running', ?, ?)
        """,
        (collection_run_id, discovery_run_id, command_hash, now.isoformat()),
    )
    conn.commit()
    try:
        result = executor.execute(command)
    except KeyboardInterrupt:
        conn.execute(
            "UPDATE trendradar_collection_run SET status='interrupted', failure_reason=?, completed_at=? WHERE collection_run_id=?",
            ("interrupted; automatic retry is forbidden", datetime.now(timezone.utc).isoformat(), collection_run_id),
        )
        conn.commit()
        raise
    except Exception as exc:
        conn.execute(
            "UPDATE trendradar_collection_run SET status='failed', failure_reason=?, completed_at=? WHERE collection_run_id=?",
            (f"executor failed without retry: {exc}", datetime.now(timezone.utc).isoformat(), collection_run_id),
        )
        conn.commit()
        return {"status": "failed", "collection_run_id": collection_run_id, "item_count": 0, "reason": str(exc)}

    if result.status != "succeeded":
        status = "timed_out" if result.status == "failed_timeout" else "failed"
        conn.execute(
            "UPDATE trendradar_collection_run SET status=?, failure_reason=?, raw_archive_ref=?, completed_at=? WHERE collection_run_id=?",
            (status, f"{result.status}; automatic retry is forbidden", result.raw_archive_ref,
             datetime.now(timezone.utc).isoformat(), collection_run_id),
        )
        conn.commit()
        return {"status": status, "collection_run_id": collection_run_id, "item_count": 0, "reason": result.status}

    raw_items = result.payload.get("items")
    if not isinstance(raw_items, list):
        raw_items = []
    inserted = 0
    invalid = 0
    for item in raw_items:
        if not isinstance(item, dict):
            invalid += 1
            continue
        provider_item_id = str(item.get("id") or item.get("source_id") or "").strip()
        title = str(item.get("title") or item.get("name") or "").strip()
        url = str(item.get("url") or item.get("source_url") or "").strip()
        channel = str(item.get("channel") or item.get("platform") or "unknown").strip()
        observed_at = str(item.get("observed_at") or now.isoformat()).strip()
        if not provider_item_id or not title or not url:
            invalid += 1
            continue
        observation_id = f"trendradar_obs_{_stable_id({'run': collection_run_id, 'id': provider_item_id})}"
        cur = conn.execute(
            """
            INSERT OR IGNORE INTO trendradar_hotspot_observation(
                observation_id, provider_item_id, title, url, source_channel, source_rank,
                observed_at, raw_json, collection_run_id
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (observation_id, provider_item_id, title, url, channel, item.get("rank"), observed_at,
             json.dumps(item, ensure_ascii=False), collection_run_id),
        )
        inserted += int(cur.rowcount > 0)
    status = "completed_with_failures" if invalid else "completed"
    failure_reason = f"{invalid} invalid normalized items retained as a counted failure" if invalid else None
    conn.execute(
        "UPDATE trendradar_collection_run SET status=?, item_count=?, failure_reason=?, raw_archive_ref=?, completed_at=? WHERE collection_run_id=?",
        (status, inserted, failure_reason, result.raw_archive_ref, datetime.now(timezone.utc).isoformat(), collection_run_id),
    )
    conn.commit()
    return {"status": status, "collection_run_id": collection_run_id, "item_count": inserted, "invalid_items": invalid}


class DailyDiscoverySourceAcquirer:
    def __init__(
        self,
        *,
        conn: sqlite3.Connection,
        trendradar_executor: ExternalCommandExecutor | None,
        mediacrawler_executor: ExternalCommandExecutor | None,
        hotspot_config: dict[str, Any],
        domain_search_config: dict[str, Any],
    ) -> None:
        self.conn = conn
        self.trendradar_executor = trendradar_executor
        self.mediacrawler_executor = mediacrawler_executor
        self.hotspot_config = hotspot_config
        self.domain_search_config = domain_search_config

    def collect_hotspots(self, *, discovery_run_id: str, now: datetime, deadline_monotonic: float | None = None) -> dict[str, Any]:
        if self.trendradar_executor is None:
            raise ValueError("TrendRadar executor is not configured for this source-specific run")
        config = dict(self.hotspot_config)
        if deadline_monotonic is not None:
            remaining = int(deadline_monotonic - time.monotonic())
            if remaining <= 0:
                return {"status": "timed_out", "item_count": 0, "reason": "batch deadline reached before TrendRadar; no request was sent"}
            config["timeout_seconds"] = min(int(config.get("timeout_seconds", 180)), remaining)
        return collect_trendradar_hotspots(
            self.conn,
            self.trendradar_executor,
            discovery_run_id=discovery_run_id,
            config=config,
            now=now,
        )

    def read_hotspot_event_detail(
        self,
        *,
        source: dict[str, Any],
        deadline_monotonic: float | None = None,
    ) -> tuple[dict[str, Any], dict[str, Any]]:
        """Attach TrendRadar's direct original-link material to one event cluster.

        Failure is final for this event in the current batch: it is not a cue to
        search, switch links, or retry the request.
        """
        enriched = json.loads(json.dumps(source, ensure_ascii=False))
        payload = enriched.get("payload") if isinstance(enriched, dict) else None
        cluster = payload.get("hotspot_event_cluster") if isinstance(payload, dict) else None
        url = str(payload.get("url") or "") if isinstance(payload, dict) else ""
        if not isinstance(cluster, dict):
            return enriched, {"status": "unavailable", "reason": "hotspot_event_cluster_missing"}
        remaining = 30
        if deadline_monotonic is not None:
            remaining = min(remaining, int(deadline_monotonic - time.monotonic()))
        detail = read_original_article(
            trendradar_dir=TRENDRADAR_INSTALL_DIR,
            url=url,
            timeout_seconds=remaining,
        )
        if detail.get("status") == "completed":
            cluster["event_detail"] = detail["event_detail"]
        return enriched, detail

    def search_tags(self, *, discovery_run_id: str, domain: str, now: datetime, deadline_monotonic: float | None = None) -> dict[str, Any]:
        if self.mediacrawler_executor is None:
            raise ValueError("platform search executor is not configured for this source-specific run")
        return run_daily_tag_searches(
            self.conn,
            self.mediacrawler_executor,
            domain_label=domain,
            run_id=discovery_run_id,
            domain_search_cfg=self.domain_search_config,
            now=now,
            deadline_monotonic=deadline_monotonic,
        )

    def acquire_daily_sources(
        self,
        *,
        discovery_run_id: str,
        domains: tuple[str, ...],
        now: datetime,
    ) -> dict[str, Any]:
        hotspot = self.collect_hotspots(discovery_run_id=discovery_run_id, now=now)
        tag_search = {
            domain: self.search_tags(discovery_run_id=discovery_run_id, domain=domain, now=now)
            for domain in domains
        }
        return {"execution_order": ["trendradar_hotspot", "tag_search"], "hotspot": hotspot, "tag_search": tag_search}
