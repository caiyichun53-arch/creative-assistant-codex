"""One complete production daily operation after a confirmed cold start."""

from __future__ import annotations

import hashlib
import json
from datetime import datetime, timezone
from typing import Any

from scripts.core.external_adapters import (
    AsrAdapter,
    LocalCompetitorMediaMaterializer,
    LocalSenseVoiceExecutor,
)
from scripts.core.external_adapters.goal_phase4_external_adapters import MediaCrawlerCollectorAdapter
from scripts.core.external_adapters.local_mediacrawler_executor import LocalMediaCrawlerExecutor
from scripts.core.external_adapters.runtime_config import external_runtime_value
from scripts.core.model_gateway.formal_skill_adapter import (
    FormalBusinessSkillAdapter,
    FormalSkillContract,
)
from scripts.core.production.business_runtime_guard import (
    enforce_daily_operations_runtime_guard,
    enforce_runtime_startup_guard,
)
from scripts.core.production.stage0_content_core import (
    DAILY_PRIORITY_REPORT_LIMIT,
    Stage0ContentProductionCore,
    StateTransitionError,
)
from scripts.core.production.stage1_competitor_registration import (
    CONTENT_SUBJECT_TYPES,
    EXPRESSION_FORMS,
    _comments_for_source,
    _filter_comments,
    COMMENT_TOP_N,
    build_production_daily_hit_gateway,
)
from scripts.core.production.stage1b_daily_discovery import (
    DAILY_REPORT_SOURCE_TYPES,
    Stage1BDailyDiscoveryService,
    build_production_daily_discovery_gateway,
    build_production_source_acquirer,
)
from scripts.core.runtime.runtime_storage import runtime_path


class ProductionDailyOperationsService:
    """Collect tracked Douyin accounts, then build the formal daily top-ten packet."""

    def __init__(
        self,
        *,
        core: Stage0ContentProductionCore,
        max_items_per_account: int = 50,
        collector: MediaCrawlerCollectorAdapter | None = None,
    ) -> None:
        if core.data_identity != "production":
            raise StateTransitionError("production daily operations require the production data identity")
        if max_items_per_account < 1 or max_items_per_account > 500:
            raise StateTransitionError("daily competitor collection size must be between 1 and 500")
        self.core = core
        self.max_items_per_account = max_items_per_account
        self.collector = collector

    def _unfinished_daily_hits(self, hit_ids: list[str]) -> list[dict[str, Any]]:
        if not hit_ids:
            return []
        placeholders = ",".join("?" for _ in hit_ids)
        rows = self.core.conn.execute(
            "SELECT DISTINCT hit.*, account.domain_label FROM hits hit "
            "JOIN competitor_accounts account ON account.account_id=hit.account_id "
            "LEFT JOIN hit_deep_analysis analysis ON analysis.hit_id=hit.hit_id "
            f"WHERE hit.hit_id IN ({placeholders}) AND analysis.hit_id IS NULL "
            "ORDER BY hit.promoted_at, hit.hit_id",
            tuple(hit_ids),
        ).fetchall()
        return [{key: row[key] for key in row.keys()} for row in rows]

    def _same_day_unfinished_hit_ids(
        self, *, domain_label: str, discovery_date: str
    ) -> list[str]:
        rows = self.core.conn.execute(
            "SELECT hit.hit_id FROM hits hit "
            "JOIN competitor_accounts account ON account.account_id=hit.account_id "
            "LEFT JOIN hit_deep_analysis analysis ON analysis.hit_id=hit.hit_id "
            "WHERE account.domain_label=? AND hit.run_id LIKE ? AND analysis.hit_id IS NULL "
            "ORDER BY hit.promoted_at, hit.hit_id",
            (domain_label, f"daily_competitor:{domain_label}:{discovery_date}:%"),
        ).fetchall()
        return [str(row["hit_id"]) for row in rows]

    def _load_prepared_daily_hit(
        self, *, hit_id: str
    ) -> tuple[str, list[dict[str, Any]]] | None:
        transcript = self.core.conn.execute(
            "SELECT cleaned_transcript_text, raw_transcript_text FROM hit_transcripts "
            "WHERE hit_id=? AND processing_status='completed' "
            "ORDER BY version DESC, created_at DESC LIMIT 1",
            (hit_id,),
        ).fetchone()
        if transcript is None:
            return None
        text = str(
            transcript["cleaned_transcript_text"]
            or transcript["raw_transcript_text"]
            or ""
        ).strip()
        if not text:
            return None
        comment_rows = self.core.conn.execute(
            "SELECT comment_id, text, like_count, sample_rank FROM hit_comments "
            f"WHERE hit_id=? ORDER BY sample_rank, comment_id LIMIT {COMMENT_TOP_N}",
            (hit_id,),
        ).fetchall()
        comments = [
            {
                "comment_id": str(row["comment_id"]),
                "text": str(row["text"]),
                "like_count": int(row["like_count"] or 0),
                "sample_rank": int(row["sample_rank"] or 0),
            }
            for row in comment_rows
        ]
        return text, comments

    def _prepare_and_break_down_daily_hits(
        self,
        *,
        hit_ids: list[str],
        collector: MediaCrawlerCollectorAdapter,
        run_id: str,
    ) -> list[dict[str, Any]]:
        """Hold daily hits until they use the same source-bound deep-breakdown path."""
        del collector, run_id
        unfinished = self._unfinished_daily_hits(hit_ids)
        return [
            {
                "hit_id": str(hit["hit_id"]),
                "status": "awaiting_deep_breakdown_pipeline",
                "automatic_retry": False,
                "detail": "daily hits do not use the retired generic breakdown format",
            }
            for hit in unfinished
        ]

    def _active_accounts(
        self, domain_label: str, *, validation_only: bool = False
    ) -> list[dict[str, Any]]:
        query = (
            "SELECT account.account_id, account.platform, account.account_name, account.homepage_url "
            "FROM competitor_accounts account "
            "JOIN stage0_competitor_registration registration "
            "ON account.source_config_ref='stage0_competitor_registration:' || registration.registration_id "
            "AND registration.data_identity=? AND registration.status='completed' "
            "JOIN stage0_cold_start_configuration configuration "
            "ON configuration.cold_start_id=registration.cold_start_id "
            "AND configuration.data_identity=registration.data_identity "
            "WHERE account.domain_label=? AND account.registration_status='active' "
            "AND configuration.status IN ('started','completed') ORDER BY account.account_id"
        )
        if validation_only:
            query += " LIMIT 1"
        rows = self.core.conn.execute(
            query, (self.core.data_identity, domain_label)
        ).fetchall()
        if not rows:
            raise StateTransitionError("daily operations require at least one formally registered tracking account")
        result = [{key: row[key] for key in row.keys()} for row in rows]
        if any(str(item["platform"]) != "douyin" for item in result):
            raise StateTransitionError("daily operations currently support confirmed Douyin accounts only")
        return result

    def _is_ready_for_candidate_discovery(self, domain_label: str) -> bool:
        row = self.core.conn.execute(
            "SELECT 1 FROM stage0_cold_start_configuration "
            "WHERE domain_label=? AND status='completed' AND data_identity=? LIMIT 1",
            (domain_label, self.core.data_identity),
        ).fetchone()
        return row is not None

    def run(
        self,
        *,
        domain_label: str,
        discovery_date: str,
        actor: str,
        attempt_ref: str,
        validation_only: bool = False,
    ) -> dict[str, Any]:
        if not actor.strip() or not attempt_ref.strip():
            raise StateTransitionError("daily operations require an actor and attempt identity")
        enforce_runtime_startup_guard(entrypoint="production_daily_operations")
        enforce_daily_operations_runtime_guard(
            entrypoint="production_daily_operations",
            source_types=DAILY_REPORT_SOURCE_TYPES,
            daily_report_limit=DAILY_PRIORITY_REPORT_LIMIT,
        )
        accounts = self._active_accounts(domain_label, validation_only=validation_only)
        collector = self.collector or MediaCrawlerCollectorAdapter(LocalMediaCrawlerExecutor(
            archive_root=runtime_path(
                "formal", "daily_operations", discovery_date, domain_label, "mediacrawler"
            ),
        ))
        collection_run_id = f"daily_competitor:{domain_label}:{discovery_date}:{attempt_ref}"
        collection_results: list[dict[str, Any]] = []
        upstream_failures: list[dict[str, Any]] = []
        new_hit_ids: list[str] = []
        for account in accounts:
            try:
                external = collector.collect_video_snapshot(
                    platform="douyin",
                    source_url=str(account["homepage_url"]),
                    max_items=self.max_items_per_account,
                    with_comments=False,
                )
                retained = self.core.record_daily_competitor_snapshot(
                    account_id=str(account["account_id"]),
                    items=tuple(external.payload["items"]),
                    raw_archive_ref=external.raw_archive_ref,
                    collection_run_id=collection_run_id,
                    observed_at=datetime.now(timezone.utc),
                )
                judgement = self.core.judge_daily_competitor_hits(
                    account_id=str(account["account_id"]),
                    source_video_ids=tuple(
                        str(item["video_id"]) for item in retained["source_refs"]
                    ),
                    run_id=collection_run_id,
                )
                new_hit_ids.extend(str(value) for value in judgement["new_hit_ids"])
                collection_results.append({
                    "account_id": account["account_id"],
                    "account_name": account["account_name"],
                    "status": "completed",
                    **retained,
                    "hit_judgement": judgement,
                })
            except Exception as exc:
                failure = {
                    "source": "daily_competitor_content",
                    "account_id": str(account["account_id"]),
                    "account_name": str(account["account_name"]),
                    "error_type": type(exc).__name__,
                    "error": str(exc),
                    "automatic_retry": False,
                }
                upstream_failures.append(failure)
                collection_results.append({**failure, "status": "failed"})

        unfinished_hit_ids = list(dict.fromkeys(
            new_hit_ids
            + self._same_day_unfinished_hit_ids(
                domain_label=domain_label,
                discovery_date=discovery_date,
            )
        ))
        hit_processing = self._prepare_and_break_down_daily_hits(
            hit_ids=unfinished_hit_ids,
            collector=collector,
            run_id=collection_run_id,
        )
        if self._is_ready_for_candidate_discovery(domain_label):
            gateway = build_production_daily_discovery_gateway(self.core)
            source_acquirer = build_production_source_acquirer(
                self.core, source_types=DAILY_REPORT_SOURCE_TYPES,
            )
            discovery = Stage1BDailyDiscoveryService(
                core=self.core, gateway=gateway, source_acquirer=source_acquirer,
            )
            result = discovery.run_daily_discovery(
                discovery_date=discovery_date,
                actor=actor,
                idempotency_key=f"agent-platform:daily:{domain_label}:{discovery_date}:{attempt_ref}",
                execution_mode="production_daily",
                domains=(domain_label,),
                source_types=DAILY_REPORT_SOURCE_TYPES,
                upstream_failures=tuple(upstream_failures),
            )
            snapshot = discovery.view_daily_snapshot(run_id=result["run_id"], domains=(domain_label,))[domain_label]
            final_status = str(result.get("status") or "completed")
        else:
            result = {"run_id": None, "status": "completed"}
            snapshot = []
            final_status = "completed"
        if upstream_failures or any(
            item.get("status") == "failed" for item in hit_processing
        ):
            final_status = "completed_with_failures"
        return {
            **result,
            "status": final_status,
            "domain_label": domain_label,
            "validation_only": validation_only,
            "account_count": len(accounts),
            "candidate_discovery": "completed" if self._is_ready_for_candidate_discovery(domain_label) else "waiting_for_domain_cold_start_completion",
            "collection": collection_results,
            "hit_processing": hit_processing,
            "candidates": snapshot,
            "candidate_count": len([item for item in snapshot if item.get("candidate_version_id")]),
        }
