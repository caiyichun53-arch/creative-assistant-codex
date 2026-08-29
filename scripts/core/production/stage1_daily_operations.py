"""One complete production daily operation after a confirmed cold start."""

from __future__ import annotations

import hashlib
import json
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

from scripts.core.external_adapters import (
    AsrAdapter,
    LocalCompetitorMediaMaterializer,
    LocalSenseVoiceExecutor,
)
from scripts.core.external_adapters.goal_phase4_external_adapters import (
    MediaCrawlerCollectorAdapter,
    local_repo_path,
)
from scripts.core.external_adapters.local_mediacrawler_executor import (
    LocalMediaCrawlerExecutor,
    retained_douyin_collector_browser_status,
)
from scripts.core.external_adapters.runtime_config import external_runtime_value
from scripts.core.production.business_runtime_guard import (
    enforce_daily_operations_runtime_guard,
    enforce_runtime_startup_guard,
)
from scripts.core.production.high_signal_policy import HISTORICAL_MATURITY_DAYS
from scripts.core.production.stage0_content_core import (
    DAILY_PRIORITY_REPORT_LIMIT,
    Stage0ContentProductionCore,
    StateTransitionError,
)
from scripts.core.production.stage1_competitor_registration import (
    CONTENT_SUBJECT_TYPES,
    EXPRESSION_FORMS,
    _comments_for_source,
    _breakdown_domain_context,
    _filter_comments,
    COMMENT_TOP_N,
    build_production_daily_hit_gateway,
)
from scripts.core.business_data.domain_labels import require_frozen_content_type_registry
from scripts.core.model_gateway.formal_skill_adapter import FormalBusinessSkillAdapter, FormalSkillContract
from scripts.core.model_gateway.formal_skill_adapter import (
    prepare_external_skill_task,
    validate_external_skill_output,
)
from scripts.core.model_gateway.model_router import ModelRouter
from scripts.core.production.stage1b_daily_discovery import (
    DAILY_REPORT_SOURCE_TYPES,
    ExternalIntelligenceRequired,
    Stage1BDailyDiscoveryService,
    build_production_source_acquirer,
)
from scripts.core.runtime.runtime_storage import runtime_path


# D7 must still be fetchable when D0 first discovered a video up to 36 hours
# after publication.  Nine days covers that boundary without turning daily
# tracking into a historical crawl.
DAILY_TRACKING_FETCH_WINDOW_DAYS = HISTORICAL_MATURITY_DAYS + 2
CHINA_TIME = timezone(timedelta(hours=8))


class ProductionDailyOperationsService:
    """Collect tracked Douyin accounts, then build the formal daily top-ten packet."""

    def __init__(
        self,
        *,
        core: Stage0ContentProductionCore,
        collector: MediaCrawlerCollectorAdapter | None = None,
        task_model_binding: dict[str, Any] | None = None,
        external_executor: Any | None = None,
    ) -> None:
        if core.data_identity != "production":
            raise StateTransitionError("production daily operations require the production data identity")
        self.core = core
        self.collector = collector
        self.task_model_binding = dict(task_model_binding or {}) or None
        self.external_executor = external_executor

    def _execution_model_binding(self) -> dict[str, Any]:
        if self.task_model_binding is None:
            self.task_model_binding = ModelRouter.from_file().resolve_current_hermes_execution_binding(
                route_id="business_analysis",
            ).as_payload()
        return dict(self.task_model_binding)

    def _unfinished_daily_hits(
        self, hit_ids: list[str], *, include_failed: bool = False
    ) -> list[dict[str, Any]]:
        if not hit_ids:
            return []
        placeholders = ",".join("?" for _ in hit_ids)
        failure_clause = "" if include_failed else (
            "AND NOT EXISTS (SELECT 1 FROM stage0_daily_hit_processing_failure failure "
            "WHERE failure.hit_id=hit.hit_id AND failure.data_identity=?) "
        )
        query_params: tuple[Any, ...] = (
            self.core.data_identity,
            self.core.data_identity,
            *hit_ids,
        )
        if not include_failed:
            query_params += (self.core.data_identity,)
        rows = self.core.conn.execute(
            "SELECT DISTINCT hit.*, account.domain_label FROM hits hit "
            "JOIN competitor_accounts account ON account.account_id=hit.account_id "
            "JOIN stage0_content_account formal_account ON formal_account.content_account_id=account.account_id "
            "AND formal_account.data_identity=? AND formal_account.account_role='competitor' AND formal_account.status='active' "
            "LEFT JOIN stage0_daily_hit_breakdown analysis ON analysis.hit_id=hit.hit_id "
            "AND analysis.data_identity=? "
            "WHERE hit.hit_id IN (" + placeholders + ") AND analysis.hit_id IS NULL "
            + failure_clause
            + "ORDER BY hit.promoted_at, hit.hit_id",
            query_params,
        ).fetchall()
        return [{key: row[key] for key in row.keys()} for row in rows]

    def _pending_daily_hit_ids(
        self,
        *,
        domain_label: str,
        business_date: str,
        daily_run_id: str,
        include_failed: bool = False,
    ) -> list[str]:
        """Return unfinished hits owned by this formal daily run only."""
        failure_clause = "" if include_failed else (
            "AND NOT EXISTS (SELECT 1 FROM stage0_daily_hit_processing_failure failure "
            "WHERE failure.hit_id=hit.hit_id AND failure.data_identity=?) "
        )
        query_params: tuple[Any, ...] = (
            self.core.data_identity,
            self.core.data_identity,
            domain_label,
            f"daily_competitor:{domain_label}:{business_date}:{daily_run_id}:%",
        )
        if not include_failed:
            query_params += (self.core.data_identity,)
        rows = self.core.conn.execute(
            "SELECT hit.hit_id FROM hits hit "
            "JOIN competitor_accounts account ON account.account_id=hit.account_id "
            "JOIN stage0_content_account formal_account ON formal_account.content_account_id=account.account_id "
            "AND formal_account.data_identity=? AND formal_account.account_role='competitor' AND formal_account.status='active' "
            "LEFT JOIN stage0_daily_hit_breakdown analysis ON analysis.hit_id=hit.hit_id "
            "AND analysis.data_identity=? "
            "WHERE account.domain_label=? AND hit.run_id LIKE ? AND analysis.hit_id IS NULL "
            + failure_clause
            + "ORDER BY hit.promoted_at, hit.hit_id",
            query_params,
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
        resume: bool = False,
        validation_only: bool = False,
    ) -> list[dict[str, Any]]:
        """Prepare each new hit, then run the source-bound formal breakdown once."""
        unfinished = self._unfinished_daily_hits(hit_ids, include_failed=resume)
        if not unfinished:
            return []
        current_content_types = None
        current_content_types_reader = getattr(
            self.core, "current_domain_content_types_frozen", None
        )
        if callable(current_content_types_reader):
            current_content_types = current_content_types_reader(
                domain_label=str(unfinished[0]["domain_label"] or "")
            )
        if not validation_only and current_content_types is False:
            return [{
                "hit_id": str(hit["hit_id"]),
                "status": "stopped",
                "stop_reason": "content_type_registry_not_current_for_activation",
                "detail": "the current domain activation has no frozen content-type registry",
                "automatic_retry": False,
            } for hit in unfinished]
        archive_root = runtime_path(
            "formal",
            "daily_operations",
            "materials",
            hashlib.sha256(run_id.encode("utf-8")).hexdigest()[:16],
            data_identity=self.core.data_identity,
        )
        media_materializer: LocalCompetitorMediaMaterializer | None = None
        transcriber: AsrAdapter | None = None
        gateway: Any | None = None
        runner: FormalBusinessSkillAdapter | None = None
        results: list[dict[str, Any]] = []
        for hit in unfinished:
            hit_id = str(hit["hit_id"])
            try:
                prepared = self._load_prepared_daily_hit(hit_id=hit_id)
                if prepared is None:
                    if media_materializer is None or transcriber is None:
                        media_materializer = LocalCompetitorMediaMaterializer(
                            archive_root=archive_root / "media",
                            ffmpeg_executable=Path(external_runtime_value("FFMPEG_PATH")),
                        )
                        transcriber = AsrAdapter(LocalSenseVoiceExecutor(
                            python_executable=Path(external_runtime_value("SENSEVOICE_PYTHON")),
                            asr_model=external_runtime_value("SENSEVOICE_ASR_MODEL"),
                            vad_model=external_runtime_value("SENSEVOICE_VAD_MODEL"),
                            archive_root=archive_root / "transcripts",
                            worker_path=Path(external_runtime_value("SENSEVOICE_WORKER")),
                        ))
                    detail = collector.collect_video_snapshots(
                        platform=str(hit["platform"]),
                        source_urls=(str(hit["url"]),),
                        with_comments=True,
                        max_comments_per_item=COMMENT_TOP_N,
                    )
                    detail_items = {
                        str(item.get("source_id") or ""): item
                        for item in detail.payload.get("items", [])
                        if isinstance(item, dict)
                    }
                    detail_item = detail_items.get(str(hit["platform_item_id"]))
                    if detail_item is None:
                        raise StateTransitionError("daily detail collection returned no matching hit")
                    comments = _filter_comments(
                        _comments_for_source(detail.payload.get("comments"), str(hit["platform_item_id"]))
                    )
                    media_urls = [
                        str(detail_item.get(value) or "").strip()
                        for value in ("music_download_url", "video_download_url")
                    ]
                    media_urls = list(dict.fromkeys(value for value in media_urls if value))
                    if not media_urls:
                        raise StateTransitionError("daily detail has no downloadable audio or video")
                    materialized = None
                    material_errors: list[str] = []
                    for media_url in media_urls:
                        try:
                            materialized = media_materializer.materialize(
                                media_url=media_url,
                                media_ref=str(hit["platform_item_id"]),
                            )
                            break
                        except Exception as exc:
                            material_errors.append(str(exc))
                    if materialized is None:
                        raise StateTransitionError(
                            "daily media download failed: " + " | ".join(material_errors)
                        )
                    transcript = transcriber.transcribe(
                        media_ref=str(hit["platform_item_id"]),
                        media_path=str(materialized.wav_path),
                        max_duration_seconds=600,
                    )
                    self.core.complete_daily_hit_material(
                        hit_id=hit_id,
                        transcript_ref=str(transcript.payload["transcript_ref"]),
                        transcript_hash=str(transcript.payload["transcript_hash"]),
                        asr_model_ref=str(transcript.payload.get("asr_model_ref") or "not_reported"),
                        vad_model_ref=str(transcript.payload.get("vad_model_ref") or "not_reported"),
                        source_media_hash=str(materialized.source_sha256),
                        comments=comments,
                        run_id=run_id,
                    )
                    prepared = self._load_prepared_daily_hit(hit_id=hit_id)
                if prepared is None:
                    raise StateTransitionError("daily hit materialization produced no usable transcript")
                transcript_text, comments = prepared
                if not validation_only:
                    try:
                        require_frozen_content_type_registry(
                            str(hit["domain_label"] or "generic")
                        )
                    except ValueError as exc:
                        if not (
                            "not frozen" in str(exc).casefold()
                            or "needs a version" in str(exc).casefold()
                        ):
                            raise
                        results.append({
                            "hit_id": hit_id,
                            "status": "stopped",
                            "stop_reason": "content_type_registry_not_frozen",
                            "detail": str(exc),
                            "automatic_retry": False,
                        })
                        return results
                input_payload = {
                    "correlation_id": f"{run_id}:{hit_id}",
                    "source_id": str(hit["platform_item_id"]),
                    "transcript": transcript_text,
                    "metrics": {
                        "like_count": int(hit["like_count"] or 0),
                        "comment_count": int(hit["comment_count"] or 0),
                        "share_count": int(hit["share_count"] or 0),
                        "collect_count": int(hit["collect_count"] or 0),
                    },
                    "comments": comments,
                    "domain_label": str(hit["domain_label"] or "generic"),
                    "domain_context": _breakdown_domain_context(
                        str(hit["domain_label"] or "generic"),
                        observed_content_types=self.core.observed_breakdown_content_types(
                            domain_label=str(hit["domain_label"] or "generic")
                        ),
                        content_type_lifecycle="classify",
                    ),
                    "schema_version": "competitor_breakdown.input.v1",
                }
                external_task, _ = prepare_external_skill_task(
                    FormalSkillContract.from_runtime_skill("competitor_breakdown"),
                    input_payload,
                    constraints={
                        "use_only_supplied_material": True,
                        "preserve_source_identity": True,
                        "cannot_change_business_state": True,
                        "do_not_search": True,
                        "no_fuzzy_evidence_matching": True,
                    },
                    business_context={
                        "hit_id": hit_id,
                        "run_id": run_id,
                        "data_identity": self.core.data_identity,
                        "origin": "daily_competitor_breakdown",
                    },
                )
                if self.external_executor is None:
                    raise ExternalIntelligenceRequired(external_task)
                submission = self.external_executor(external_task)
                external_output = submission.get("output") if isinstance(submission, dict) else None
                model_run_id = self.core.record_external_daily_hit_execution(
                    hit_id=hit_id,
                    execution_id=str(submission.get("execution_id") or "") if isinstance(submission, dict) else "",
                    executor_id=str(submission.get("executor_id") or "") if isinstance(submission, dict) else "",
                    model_ref=str(submission.get("model_ref") or "") if isinstance(submission, dict) else None,
                    submitted_at=str(submission.get("submitted_at") or "") if isinstance(submission, dict) else None,
                    input_payload=input_payload,
                    output_payload=external_output,
                )
                try:
                    output = validate_external_skill_output(
                        FormalSkillContract.from_runtime_skill("competitor_breakdown"),
                        input_payload,
                        external_output,
                    )
                except Exception as exc:
                    self.core.conn.execute(
                        "UPDATE stage0_daily_hit_model_run SET error_json=? WHERE daily_hit_model_run_id=?",
                        (json.dumps({"validation_error": str(exc)}, ensure_ascii=False), model_run_id),
                    )
                    raise
                breakdown = self.core.record_daily_hit_breakdown(
                    hit_id=hit_id,
                    artifact=output,
                    model_run_id=model_run_id,
                )
                question_expansions = self.core.register_breakdown_question_expansions(
                    domain_label=str(hit["domain_label"] or "generic"),
                    breakdown=output,
                    parent_source_ref={
                        "source_type": "hit_breakdown",
                        "source_object_id": hit_id,
                        "source_object_version": model_run_id,
                    },
                    actor="daily_hit_breakdown",
                    content_type_lifecycle="classify",
                )
                results.append({
                    "hit_id": hit_id,
                    "status": "completed",
                    "material": "completed",
                    "breakdown": breakdown,
                    "question_expansions": question_expansions,
                    "automatic_retry": False,
                })
            except ExternalIntelligenceRequired as exc:
                results.append({
                    "hit_id": hit_id,
                    "status": "requires_external_intelligence",
                    "task": exc.task,
                    "material": "completed",
                    "automatic_retry": False,
                })
                continue
            except Exception as exc:
                try:
                    self.core.record_daily_hit_processing_failure(
                        hit_id=hit_id,
                        stage_name="daily_material_and_breakdown",
                        error={
                            "error_type": type(exc).__name__,
                            "reason": str(exc),
                            "automatic_retry": False,
                        },
                        run_id=run_id,
                    )
                except Exception:
                    pass
                results.append({
                    "hit_id": hit_id,
                    "status": "failed",
                    "automatic_retry": False,
                    "detail": str(exc),
                })
        return results

    def _active_accounts(
        self, domain_label: str, *, validation_only: bool = False, account_id: str | None = None
    ) -> list[dict[str, Any]]:
        activation_reader = getattr(self.core, "get_current_domain_activation", None)
        if not callable(activation_reader):
            raise StateTransitionError(
                "daily operations require an activation-aware Core"
            )
        activation = activation_reader(domain_label=domain_label)
        if activation is None:
            raise StateTransitionError(
                "daily operations require a current cold-start activation"
            )
        query = (
            "SELECT account.account_id, account.platform, account.account_name, account.homepage_url "
            "FROM competitor_accounts account "
            "JOIN stage0_content_account formal_account ON formal_account.content_account_id=account.account_id "
            "AND formal_account.data_identity=? AND formal_account.account_role='competitor' AND formal_account.status='active' "
            "JOIN stage0_competitor_registration registration "
            "ON account.source_config_ref='stage0_competitor_registration:' || registration.registration_id "
            "AND registration.data_identity=? AND registration.status='completed' "
            "JOIN stage0_cold_start_configuration configuration "
            "ON configuration.cold_start_id=registration.cold_start_id "
            "AND configuration.data_identity=registration.data_identity "
            "WHERE account.domain_label=? AND account.registration_status='active' "
            "AND configuration.status IN ('started','completed')"
        )
        params: list[str] = [self.core.data_identity, self.core.data_identity, domain_label]
        if activation is not None:
            query += " AND registration.cold_start_id=?"
            params.append(str(activation["cold_start_id"]))
        if validation_only:
            if not str(account_id or "").strip():
                raise StateTransitionError("single-account validation requires an explicit account")
            query += " AND account.account_id=?"
            params.append(str(account_id).strip())
        else:
            query += " ORDER BY account.account_id"
        rows = self.core.conn.execute(query, tuple(params)).fetchall()
        if not rows:
            raise StateTransitionError("daily operations require at least one formally registered tracking account")
        result = [{key: row[key] for key in row.keys()} for row in rows]
        if any(str(item["platform"]) != "douyin" for item in result):
            raise StateTransitionError("daily operations currently support confirmed Douyin accounts only")
        return result

    def _is_ready_for_candidate_discovery(self, domain_label: str) -> bool:
        activation_reader = getattr(self.core, "get_current_domain_activation", None)
        if not callable(activation_reader):
            return False
        activation = activation_reader(domain_label=domain_label)
        if activation is None:
            return False
        if activation is not None:
            row = self.core.conn.execute(
                "SELECT 1 FROM stage0_cold_start_configuration "
                "WHERE configuration_id=? AND cold_start_id=? "
                "AND status='completed' AND data_identity=? LIMIT 1",
                (
                    activation["configuration_id"],
                    activation["cold_start_id"],
                    self.core.data_identity,
                ),
            ).fetchone()
        return row is not None

    def run_candidate_discovery(
        self,
        *,
        domain_label: str,
        discovery_date: str,
        actor: str,
        attempt_ref: str,
        daily_run_id: str,
        resume: bool = False,
        validation_only: bool = False,
        upstream_failures: tuple[dict[str, Any], ...] = (),
        effective_at: datetime | None = None,
    ) -> dict[str, Any]:
        """Run the candidate path after collection has finished.

        Collection and candidate discovery are deliberately separate business
        actions.  A single-account collection validation must never enter this
        path, because it is only checking source registration and deduplication.
        """
        if not actor.strip() or not attempt_ref.strip():
            raise StateTransitionError("candidate discovery requires an actor and attempt identity")
        if not validation_only and not str(daily_run_id).strip():
            raise StateTransitionError("candidate discovery requires a formal daily run")
        enforce_runtime_startup_guard(
            entrypoint="production_daily_candidate_discovery",
            scope="daily",
            data_identity=self.core.data_identity,
        )
        enforce_daily_operations_runtime_guard(
            entrypoint="production_daily_candidate_discovery",
            source_types=DAILY_REPORT_SOURCE_TYPES,
            daily_report_limit=DAILY_PRIORITY_REPORT_LIMIT,
            data_identity=self.core.data_identity,
        )
        if validation_only:
            return {
                "run_id": None,
                "status": "completed",
                "candidate_discovery": "not_triggered_validation_only",
                "candidates": [],
                "candidate_count": 0,
            }
        if not self._is_ready_for_candidate_discovery(domain_label):
            return {
                "run_id": None,
                "status": "stopped",
                "candidate_discovery": "waiting_for_domain_cold_start_completion",
                "candidates": [],
                "candidate_count": 0,
                "stop_reason": "domain_cold_start_not_completed",
            }
        existing = self.core.daily_candidate_discovery_run(daily_run_id=daily_run_id)
        if existing is not None and str(existing.get("lifecycle_status") or "") == "completed":
            discovery = Stage1BDailyDiscoveryService(
                core=self.core, gateway=None,
            )
            snapshot = discovery.view_daily_snapshot(
                run_id=str(existing["run_id"]), domains=(domain_label,)
            )[domain_label]
            return {
                "run_id": str(existing["run_id"]),
                "status": str(existing["lifecycle_status"]),
                "candidate_discovery": "already_completed",
                "candidates": snapshot,
                "candidate_count": len(
                    [item for item in snapshot if item.get("candidate_version_id")]
                ),
                "failure_reason": existing.get("failure_reason"),
                "technical_failures": 0,
                "failure_details": [],
                "resumed": bool(resume),
            }
        source_acquirer = build_production_source_acquirer(
            self.core, source_types=DAILY_REPORT_SOURCE_TYPES,
        )
        discovery = Stage1BDailyDiscoveryService(
            core=self.core,
            gateway=None,
            source_acquirer=source_acquirer,
            external_executor=self.external_executor,
        )
        result = discovery.run_daily_discovery(
            discovery_date=discovery_date,
            actor=actor,
            idempotency_key=f"agent-platform:daily-candidates:{domain_label}:{discovery_date}:{attempt_ref}",
            execution_mode="production_daily",
            daily_run_id=daily_run_id,
            domains=(domain_label,),
            source_types=DAILY_REPORT_SOURCE_TYPES,
            upstream_failures=upstream_failures,
            now=effective_at,
        )
        snapshot = discovery.view_daily_snapshot(
            run_id=result["run_id"], domains=(domain_label,)
        )[domain_label]
        return {
            **result,
            "candidate_discovery": "completed",
            "candidates": snapshot,
            "candidate_count": len([item for item in snapshot if item.get("candidate_version_id")]),
        }

    def run(
        self,
        *,
        domain_label: str,
        discovery_date: str,
        actor: str,
        attempt_ref: str,
        daily_run_id: str | None = None,
        resume: bool = False,
        validation_only: bool = False,
        account_id: str | None = None,
        effective_at: datetime | None = None,
    ) -> dict[str, Any]:
        if not actor.strip() or not attempt_ref.strip():
            raise StateTransitionError("daily operations require an actor and attempt identity")
        enforce_runtime_startup_guard(
            entrypoint="production_daily_operations",
            scope="daily",
            data_identity=self.core.data_identity,
        )
        enforce_daily_operations_runtime_guard(
            entrypoint="production_daily_operations",
            source_types=DAILY_REPORT_SOURCE_TYPES,
            daily_report_limit=DAILY_PRIORITY_REPORT_LIMIT,
            data_identity=self.core.data_identity,
        )
        if not validation_only and not str(daily_run_id or "").strip():
            raise StateTransitionError("production daily operations require a formal daily run")
        accounts = self._active_accounts(
            domain_label, validation_only=validation_only, account_id=account_id
        )
        if self.collector is None:
            browser_status = retained_douyin_collector_browser_status(
                local_repo_path("vendor", "MediaCrawler"),
                data_identity=self.core.data_identity,
            )
            if browser_status["status"] != "ready":
                failure = {
                    "source": "daily_competitor_collection_preflight",
                    "error_type": "ExternalAdapterError",
                    "error": (
                        "the reusable Douyin collector browser is not ready; "
                        "daily collection was stopped before account iteration"
                    ),
                    "browser_status": browser_status,
                    "automatic_retry": False,
                }
                return {
                    "run_id": None,
                    "collection_run_id": None,
                    "status": "completed_with_failures",
                    "domain_label": domain_label,
                    "validation_only": validation_only,
                    "account_count": len(accounts),
                    "candidate_discovery": "not_triggered",
                    "collection": [],
                    "hit_processing": [],
                    "candidates": [],
                    "candidate_count": 0,
                    "upstream_failures": [failure],
                }
        collector = self.collector or MediaCrawlerCollectorAdapter(LocalMediaCrawlerExecutor(
            archive_root=runtime_path(
                "formal", "daily_operations", discovery_date, domain_label, "mediacrawler",
                data_identity=self.core.data_identity,
            ),
            data_identity=self.core.data_identity,
        ))
        formal_daily_run_id = str(daily_run_id or "").strip()
        collection_run_id = (
            f"daily_competitor:{domain_label}:{discovery_date}:"
            f"{formal_daily_run_id}:{attempt_ref}"
        )
        collection_results: list[dict[str, Any]] = []
        upstream_failures: list[dict[str, Any]] = []
        new_hit_ids: list[str] = []
        observed_at = effective_at or datetime.now(timezone.utc)
        if observed_at.tzinfo is None:
            raise StateTransitionError("effective observation time must include a timezone")
        observed_at = observed_at.astimezone(timezone.utc)
        if observed_at.astimezone(CHINA_TIME).date().isoformat() != discovery_date:
            raise StateTransitionError(
                "effective observation time must belong to the daily business date"
            )
        daily_cutoff = observed_at - timedelta(days=DAILY_TRACKING_FETCH_WINDOW_DAYS)
        completed_account_ids = (
            self.core.daily_collection_account_ids(
                daily_run_id=formal_daily_run_id,
            )
            if resume
            else set()
        )
        for account in accounts:
            account_id = str(account["account_id"])
            if account_id in completed_account_ids:
                collection_results.append({
                    "account_id": account_id,
                    "account_name": account["account_name"],
                    "status": "reused_completed_snapshot",
                })
                continue
            try:
                external = collector.collect_video_snapshot(
                    platform="douyin",
                    source_url=str(account["homepage_url"]),
                    max_items=None,
                    with_comments=False,
                    published_after=daily_cutoff,
                )
                retained = self.core.record_daily_competitor_snapshot(
                    account_id=str(account["account_id"]),
                    items=tuple(external.payload["items"]),
                    raw_archive_ref=external.raw_archive_ref,
                    collection_run_id=collection_run_id,
                    observed_at=observed_at,
                    business_date=discovery_date,
                    daily_run_id=formal_daily_run_id or None,
                )
                judgement = self.core.judge_daily_competitor_hits(
                    account_id=str(account["account_id"]),
                    source_video_ids=tuple(
                        str(item["video_id"]) for item in retained["source_refs"]
                    ),
                    run_id=collection_run_id,
                    evaluated_at=observed_at,
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
            + self._pending_daily_hit_ids(
                domain_label=domain_label,
                business_date=discovery_date,
                daily_run_id=formal_daily_run_id,
                include_failed=resume,
            )
        ))
        hit_processing = self._prepare_and_break_down_daily_hits(
            hit_ids=unfinished_hit_ids,
            collector=collector,
            run_id=collection_run_id,
            resume=resume,
            validation_only=validation_only,
        )
        final_status = "completed"
        if upstream_failures or any(
            item.get("status") == "failed" for item in hit_processing
        ):
            final_status = "completed_with_failures"
        elif any(item.get("status") == "stopped" for item in hit_processing):
            final_status = "stopped"
        stop_reason = next(
            (
                str(item.get("stop_reason"))
                for item in hit_processing
                if item.get("status") == "stopped" and item.get("stop_reason")
            ),
            None,
        )
        return {
            "run_id": formal_daily_run_id or None,
            "collection_run_id": collection_run_id,
            "status": final_status,
            "domain_label": domain_label,
            "validation_only": validation_only,
            "account_count": len(accounts),
            "candidate_discovery": "not_triggered",
            "collection": collection_results,
            "hit_processing": hit_processing,
            "candidates": [],
            "candidate_count": 0,
            "upstream_failures": upstream_failures,
            "stop_reason": stop_reason,
        }
