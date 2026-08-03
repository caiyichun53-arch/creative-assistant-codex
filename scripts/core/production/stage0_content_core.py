"""Stage 0's only formal write boundary for the first content-production chain.

This module deliberately implements state, version, audit, input-assembly, and
data-identity controls only.  It does not generate research or content.
"""

from __future__ import annotations

import hashlib
import json
import math
import re
import sqlite3
import uuid
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Literal

from scripts.core.execution_contract import require_baseline_citations
from scripts.core.business_data.domain_labels import (
    formal_domain_labels,
    get_content_workflow_mode,
    get_discovery_policy,
)
from scripts.core.model_gateway.goal07_model_gateway import ModelRequest, ModelRunEnvelope
from scripts.core.model_gateway.model_router import ModelRouter, ModelRouterError
from scripts.core.production.high_signal_policy import (
    FIRST_REGISTRATION_COLLECTION_POLICY_VERSION,
    FIRST_REGISTRATION_MAX_ITEMS,
    HIGH_SIGNAL_POLICY_VERSION,
    HISTORICAL_MATURITY_DAYS,
    HISTORICAL_METRICS,
    MATURE_HISTORY_WINDOW_DAYS,
    MIN_RELIABLE_HISTORY_ITEMS,
    build_historical_collection_artifact,
    build_high_signal_artifact,
    judge_against_formal_d_baseline,
    judge_against_mature_history,
    validate_historical_collection_artifact,
    validate_high_signal_artifact,
)
from scripts.core.production.business_runtime_guard import (
    enforce_atomic_skill_runtime_guard,
    enforce_runtime_startup_guard,
    record_runtime_guard_event,
)
from scripts.core.runtime.runtime_storage import formal_database_path


ROOT = Path(__file__).resolve().parents[3]
FORMAL_DB_PATH = formal_database_path()
DataIdentity = Literal["production", "test", "fixture", "synthetic", "replay", "mock"]
NON_PRODUCTION_IDENTITIES = frozenset({"test", "fixture", "synthetic", "replay", "mock"})
DISCOVERY_EXECUTION_MODES = frozenset({"test_isolated", "real_daily_validation", "production_daily"})
DISCOVERY_RUN_OUTCOMES = frozenset(
    {"processing", "completed", "completed_with_failures", "timed_out", "interrupted", "failed", "cancelled"}
)
MANUAL_SOURCE_KINDS = frozenset({"direction", "link", "person", "work", "playlist"})
MANUAL_SOURCE_TARGET_KINDS = frozenset(
    {"saved_user_direction", "candidate", "formal_topic", "person_exploration", "work_exploration", "playlist_exploration"}
)
CONTENT_ACCOUNT_ROLES = frozenset({"owned", "competitor"})
COLD_START_COMPETITOR_MIN = 10
COLD_START_COMPETITOR_MAX = 20
COMPETITOR_REGISTRATION_STEPS = (
    "historical_material",
    "high_signal_identification",
    "transcripts_and_comments",
    "breakdown",
    "tag_candidates",
)
CANDIDATE_SCORE_WEIGHTS = {
    "demand_strength": 22,
    "information_increment": 22,
    "expression_pull": 18,
    "distinct_angle": 16,
    "question_focus": 12,
    "timeliness": 10,
}
DAILY_PRIORITY_REPORT_LIMIT = 10
_SOURCE_HASHTAG_PATTERN = re.compile(r"#([^#\s]+)")
_TAG_EDGE_PUNCTUATION = "，,。.！!?？:：;；、|/\\()（）[]【】<>《》“”'\"`~·…"

CHAIN_NODES = (
    "formal_topic",
    "research_plan",
    "research_plan_confirmation",
    "deep_research",
    "research_result_confirmation",
    "content_plan",
    "content_plan_confirmation",
    "formal_draft",
    "initial_draft_confirmation",
    "copy_optimization",
    "de_ai_revision",
    "review",
    "user_final_confirmation",
)
ARTIFACT_NODES = frozenset(
    {
        "formal_topic",
        "research_plan",
        "deep_research",
        "content_plan",
        "formal_draft",
        "copy_optimization",
        "de_ai_revision",
        "review",
    }
)
UPSTREAM_NODE = {
    "research_plan": "formal_topic",
    "deep_research": "research_plan",
    "content_plan": "deep_research",
    "formal_draft": "content_plan",
    "copy_optimization": "formal_draft",
    "de_ai_revision": "copy_optimization",
    "review": "de_ai_revision",
}
CONFIRMATION_NODE = {
    "research_plan": "research_plan_confirmation",
    "deep_research": "research_result_confirmation",
    "content_plan": "content_plan_confirmation",
    "formal_draft": "initial_draft_confirmation",
    "review": "user_final_confirmation",
}
NEXT_ARTIFACT_NODE = {
    "formal_topic": "research_plan",
    "research_plan": "deep_research",
    "deep_research": "content_plan",
    "content_plan": "formal_draft",
    "formal_draft": "copy_optimization",
    "copy_optimization": "de_ai_revision",
    "de_ai_revision": "review",
}
MODEL_BINDINGS = {
    "research_plan": ("business_analysis", "business_planning"),
    "deep_research": ("business_analysis", "material_summary"),
    "content_plan": ("business_analysis", "business_planning"),
    "formal_draft": ("writing_generation", "rough_draft"),
    "copy_optimization": ("writing_generation", "polishing"),
    "de_ai_revision": ("writing_generation", "de_ai_style"),
    "review": ("writing_generation", "final_copy_review"),
}


class Stage0CoreError(RuntimeError):
    pass


class DataIdentityError(Stage0CoreError):
    pass


class StateTransitionError(Stage0CoreError):
    pass


class StaleResultError(Stage0CoreError):
    pass


class ModelGatewayRequiredError(Stage0CoreError):
    pass


class ModelBindingUnavailableError(Stage0CoreError):
    pass


class LegacyProductionEntryDisabledError(Stage0CoreError):
    pass


@dataclass(frozen=True)
class InputAssembly:
    task_id: str
    node: str
    upstream_version_id: str | None
    user_requirements: str
    material_refs: tuple[dict[str, Any], ...]
    research_refs: tuple[dict[str, Any], ...]
    content_plan_ref: dict[str, Any] | None
    considered_experience: tuple[dict[str, Any], ...]
    adopted_experience: tuple[dict[str, Any], ...]
    rejected_experience: tuple[dict[str, Any], ...]
    omitted_materials: tuple[dict[str, Any], ...]
    prompt_version: str
    skill_version: str
    model_config_version: str

    def payload(self) -> dict[str, Any]:
        return {
            "task_id": self.task_id,
            "node": self.node,
            "upstream_version_id": self.upstream_version_id,
            "user_requirements": self.user_requirements,
            "material_refs": list(self.material_refs),
            "research_refs": list(self.research_refs),
            "content_plan_ref": self.content_plan_ref,
            "considered_experience": list(self.considered_experience),
            "adopted_experience": list(self.adopted_experience),
            "rejected_experience": list(self.rejected_experience),
            "omitted_materials": list(self.omitted_materials),
            "prompt_version": self.prompt_version,
            "skill_version": self.skill_version,
            "model_config_version": self.model_config_version,
        }


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _id(prefix: str) -> str:
    return f"{prefix}_{uuid.uuid4().hex}"


def _canonical(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def _hash(value: Any) -> str:
    return hashlib.sha256(_canonical(value).encode("utf-8")).hexdigest()


def _contains_build_root_path(value: Any) -> bool:
    if isinstance(value, dict):
        return any(_contains_build_root_path(item) for item in value.values())
    if isinstance(value, list):
        return any(_contains_build_root_path(item) for item in value)
    if not isinstance(value, str):
        return False
    normalized = value.replace("/", "\\").casefold()
    build_root = str(ROOT.resolve()).replace("/", "\\").casefold()
    return normalized == build_root or normalized.startswith(build_root + "\\")


def _hide_build_root_paths(value: Any, *, key: str = "") -> Any:
    """Hide stale build-root references when immutable history is read.

    Historical registration records remain unchanged for auditability, but a
    cleaned runtime must never expose their removed archive paths as usable
    inputs.
    """
    if isinstance(value, dict):
        return {name: _hide_build_root_paths(item, key=name) for name, item in value.items()}
    if isinstance(value, list):
        return [_hide_build_root_paths(item, key=key) for item in value]
    if not isinstance(value, str) or not _contains_build_root_path(value):
        return value
    lowered = key.casefold()
    if lowered.endswith("_ref") or lowered.endswith("_path") or lowered in {"path", "database", "backup_ref"}:
        return None
    return "build_path_removed"


def _experience_context_matches(
    *, context_text: str, applicable_when: list[str]
) -> list[str]:
    """Use only explicit wording overlap; never infer that an experience is relevant."""
    compact_context = re.sub(r"[^\u4e00-\u9fffA-Za-z0-9]", "", context_text).casefold()
    if not compact_context:
        return []
    matches: list[str] = []
    for condition in applicable_when:
        compact_condition = re.sub(r"[^\u4e00-\u9fffA-Za-z0-9]", "", condition).casefold()
        if len(compact_condition) < 4:
            continue
        # A four-character (or longer) literal slice keeps this deliberately
        # conservative for Chinese text without pretending to understand a
        # topic from broad domain labels alone.
        found = any(
            compact_condition[start : start + width] in compact_context
            for width in range(min(8, len(compact_condition)), 3, -1)
            for start in range(0, len(compact_condition) - width + 1)
        )
        if found:
            matches.append(condition)
    return matches


def _deterministic_named_entity_filter_reason(
    tag: str,
    *,
    topic_policy: dict[str, Any],
) -> str:
    """Reject person and other named-entity tags without a model call."""
    normalized = tag.strip().casefold()
    if not normalized:
        return ""
    allowed_concepts = {
        str(value).strip().casefold()
        for value in topic_policy.get("person_name_concept_allowlist", [])
        if str(value).strip()
    }
    if normalized in allowed_concepts:
        return ""
    named_entities = {
        str(value).strip().casefold()
        for value in topic_policy.get("named_entity_terms", [])
        if str(value).strip()
    }
    if any(entity in normalized for entity in named_entities):
        return "人物、组合或机构名称，不作为领域话题标签"
    work_titles = {
        str(value).strip().casefold()
        for value in topic_policy.get("work_title_terms", [])
        if str(value).strip()
    }
    if any(title == normalized for title in work_titles):
        return "作品名称，不作为领域话题标签"
    return ""


def _hotspot_cluster_key(title: str) -> str:
    normalized = "".join(ch for ch in title.casefold() if ch.isalnum())
    return normalized or _hash({"title": title})[:20]


def _hotspot_rank_value(row: sqlite3.Row) -> int:
    value = row["source_rank"]
    return int(value) if isinstance(value, int) or str(value).isdigit() else 9999


def _hotspot_time_sort_value(value: str) -> float:
    try:
        return datetime.fromisoformat(value.replace("Z", "+00:00")).timestamp()
    except ValueError:
        return 0.0


def _build_hotspot_event_cluster_sources(
    rows: list[sqlite3.Row], *, per_source_limit: int | None
) -> list[dict[str, Any]]:
    clusters: dict[str, list[sqlite3.Row]] = {}
    for row in rows:
        title = str(row["title"] or "").strip()
        url = str(row["url"] or "").strip()
        if not title or not url:
            continue
        clusters.setdefault(_hotspot_cluster_key(title), []).append(row)

    queued: list[tuple[int, int, int, str, list[sqlite3.Row]]] = []
    for key, members in clusters.items():
        ranks = [_hotspot_rank_value(row) for row in members]
        platform_count = len({str(row["source_channel"]) for row in members})
        # Select review signals by their hotspot strength, never crawl time.
        queued.append((-platform_count, min(ranks), sum(ranks), key, members))
    queued.sort(key=lambda item: item[:4])

    result: list[dict[str, Any]] = []
    selected_clusters = queued if per_source_limit is None else queued[:per_source_limit]
    for negative_platform_count, best_rank, rank_sum, key, members in selected_clusters:
        representative = min(members, key=_hotspot_rank_value)
        observed_at = max(str(row["observed_at"]) for row in members)
        merged_sources = [
            {
                "observation_id": str(row["observation_id"]),
                "title": str(row["title"]),
                "url": str(row["url"]),
                "channel": str(row["source_channel"]),
                "rank": row["source_rank"],
                "observed_at": str(row["observed_at"]),
                "trendradar_record": json.loads(str(row["raw_json"])),
            }
            for row in sorted(members, key=_hotspot_rank_value)
        ]
        cluster_payload = {
            "cluster_id": f"hotspot_cluster_{_hash({'key': key})[:20]}",
            "representative_source": str(representative["title"]),
            "merged_sources": merged_sources,
            "dedupe_reason": "deterministic title-normalized global event cluster",
            "selection_signal": {
                "supporting_platform_count": -negative_platform_count,
                "best_original_rank": best_rank,
                "original_rank_sum": rank_sum,
                "rule": "more supporting platforms first; then better original ranks; then stable event id",
            },
        }
        source_content = "；".join(dict.fromkeys(item["title"] for item in merged_sources[:8]))
        representative_raw_hash = _hash(json.loads(str(representative["raw_json"])))
        result.append(
            {
                "source_type": "hotspot",
                "source_object_id": str(representative["observation_id"]),
                "source_object_version": str(representative["observed_at"]),
                "source_time": str(representative["observed_at"]),
                "payload": {
                    "source_id": cluster_payload["cluster_id"],
                    "title": str(representative["title"]),
                    "url": str(representative["url"]),
                    "account_name": "TrendRadar/event_cluster",
                    "source_time": observed_at,
                    "hotspot_event_cluster": cluster_payload,
                    "material_packet": {
                        "fact_summary": source_content,
                        "key_source_refs": [item["url"] for item in merged_sources[:8]],
                        "audience_relation": "是否与目标受众相关由一次热点机会判断决定",
                        "domain_fit": "热点不按标题词预先归入领域",
                        "possible_questions": [item["title"] for item in merged_sources[:5]],
                        "uncertainty": "cluster is still a discovery source; formal research evidence has not started",
                        "material_gaps": ["热点只提供发现材料；正式研究证据尚未开始。"],
                        "risks": [],
                        "stop_reason": "none",
                    },
                    "formal_source": {
                        "table": "trendradar_hotspot_observation",
                        "object_id": str(representative["observation_id"]),
                        "object_version": str(representative["observed_at"]),
                        "raw_metadata_hash": representative_raw_hash,
                    },
                },
            }
        )
    return result


def is_formal_database(path: Path | str) -> bool:
    return Path(path).resolve() == FORMAL_DB_PATH.resolve()


def reject_legacy_cli_production_write(path: Path | str, entrypoint: str) -> None:
    """Fail closed before a legacy CLI can open the formal Stage 0 database."""
    if is_formal_database(path):
        raise LegacyProductionEntryDisabledError(
            f"{entrypoint} is not a formal production entrypoint; use Stage0ContentProductionCore"
        )


def require_legacy_test_identity(data_identity: str) -> str:
    if data_identity not in NON_PRODUCTION_IDENTITIES:
        raise DataIdentityError("legacy CLI may only run with an explicit non-production data identity")
    return data_identity


class CoreModelRunMaterializer:
    """ModelGateway materializer that persists envelopes through Stage 0 Core."""

    def __init__(self, core: "Stage0ContentProductionCore") -> None:
        self._core = core

    def persist_envelope(self, envelope: ModelRunEnvelope) -> str:
        metadata = dict(envelope.metadata or {})
        binding = metadata.get("stage0_core")
        if not isinstance(binding, dict):
            raise ModelGatewayRequiredError("ModelGateway request lacks Stage 0 Core binding")
        return self._core._persist_gateway_envelope(envelope, binding)


class CoreDiscoveryModelRunMaterializer:
    """ModelGateway materializer for Stage 1B candidates; never creates a production task."""

    def __init__(self, core: "Stage0ContentProductionCore") -> None:
        self._core = core

    def persist_envelope(self, envelope: ModelRunEnvelope) -> str:
        binding = dict((envelope.metadata or {}).get("stage1b_core") or {})
        if not binding:
            raise ModelGatewayRequiredError("ModelGateway request lacks Stage 1B Core binding")
        return self._core.persist_discovery_model_envelope(envelope, binding)


class CoreExperienceCandidateModelRunMaterializer:
    """Persist one source-frozen experience-candidate model call without publishing experience."""

    def __init__(self, core: "Stage0ContentProductionCore") -> None:
        self._core = core

    def persist_envelope(self, envelope: ModelRunEnvelope) -> str:
        binding = dict((envelope.metadata or {}).get("experience_candidate_core") or {})
        if not binding:
            raise ModelGatewayRequiredError("ModelGateway request lacks experience-candidate Core binding")
        return self._core._persist_experience_candidate_gateway_envelope(envelope, binding)


class CoreCompetitorRegistrationModelRunMaterializer:
    """Persist model calls used by competitor breakdown and tag extraction."""

    def __init__(self, core: "Stage0ContentProductionCore") -> None:
        self._core = core

    def persist_envelope(self, envelope: ModelRunEnvelope) -> str:
        binding = dict((envelope.metadata or {}).get("competitor_registration_core") or {})
        if not binding:
            raise ModelGatewayRequiredError("ModelGateway request lacks competitor-registration Core binding")
        return self._core._persist_competitor_registration_gateway_envelope(envelope, binding)


class CoreDailyHitModelRunMaterializer:
    """Persist one daily-hit breakdown without pretending it is a registration step."""

    def __init__(self, core: "Stage0ContentProductionCore") -> None:
        self._core = core

    def persist_envelope(self, envelope: ModelRunEnvelope) -> str:
        binding = dict((envelope.metadata or {}).get("daily_hit_core") or {})
        if not binding:
            raise ModelGatewayRequiredError("ModelGateway request lacks daily-hit Core binding")
        return self._core._persist_daily_hit_gateway_envelope(envelope, binding)


class Stage0ContentProductionCore:
    """The sole formal Core API for Stage 0's first vertical production chain."""

    def __init__(self, connection: sqlite3.Connection, *, db_path: Path, data_identity: DataIdentity) -> None:
        self.conn = connection
        self.db_path = db_path.resolve()
        self.data_identity = data_identity
        self.conn.row_factory = sqlite3.Row

    @classmethod
    def open(cls, db_path: Path | str, *, data_identity: DataIdentity) -> "Stage0ContentProductionCore":
        resolved = Path(db_path).resolve()
        if data_identity == "production":
            require_baseline_citations(["3", "4", "5", "6", "7", "11", "12", "13"])
            if resolved != FORMAL_DB_PATH.resolve():
                raise DataIdentityError("production identity may only use the configured formal runtime database")
        elif data_identity in NON_PRODUCTION_IDENTITIES:
            if resolved == FORMAL_DB_PATH.resolve():
                raise DataIdentityError("non-production identity must never open the formal production database")
            try:
                resolved.relative_to(ROOT.resolve())
            except ValueError:
                pass
            else:
                raise DataIdentityError("non-production identity must not open a database inside the build root")
        else:
            raise DataIdentityError(f"unsupported data identity: {data_identity}")
        connection = sqlite3.connect(resolved)
        core = cls(connection, db_path=resolved, data_identity=data_identity)
        core.install_schema()
        return core

    def close(self) -> None:
        self.conn.close()

    def install_schema(self) -> None:
        self.conn.executescript(
            """
            CREATE TABLE IF NOT EXISTS stage0_content_task (
                task_id TEXT PRIMARY KEY,
                topic_version_id TEXT NOT NULL,
                current_node TEXT NOT NULL,
                current_version_id TEXT,
                current_status TEXT NOT NULL,
                task_revision INTEGER NOT NULL,
                data_identity TEXT NOT NULL,
                created_by TEXT NOT NULL,
                created_at TEXT NOT NULL,
                cancelled_reason TEXT
            );
            CREATE TABLE IF NOT EXISTS stage0_content_node_version (
                version_id TEXT PRIMARY KEY,
                task_id TEXT NOT NULL REFERENCES stage0_content_task(task_id),
                node TEXT NOT NULL,
                parent_version_id TEXT,
                upstream_version_id TEXT,
                input_assembly_id TEXT,
                status TEXT NOT NULL,
                output_ref TEXT,
                validation_status TEXT NOT NULL,
                data_identity TEXT NOT NULL,
                created_by TEXT NOT NULL,
                created_at TEXT NOT NULL,
                task_revision INTEGER NOT NULL
            );
            CREATE TABLE IF NOT EXISTS stage0_content_decision (
                decision_id TEXT PRIMARY KEY,
                task_id TEXT NOT NULL REFERENCES stage0_content_task(task_id),
                node TEXT NOT NULL,
                version_id TEXT NOT NULL REFERENCES stage0_content_node_version(version_id),
                decision TEXT NOT NULL,
                actor TEXT NOT NULL,
                actor_kind TEXT NOT NULL,
                reason TEXT NOT NULL,
                created_at TEXT NOT NULL,
                data_identity TEXT NOT NULL
            );
            CREATE TABLE IF NOT EXISTS stage0_content_node_failure (
                failure_id TEXT PRIMARY KEY,
                task_id TEXT NOT NULL REFERENCES stage0_content_task(task_id),
                failed_version_id TEXT NOT NULL UNIQUE REFERENCES stage0_content_node_version(version_id),
                request_version_id TEXT NOT NULL UNIQUE REFERENCES stage0_content_node_version(version_id),
                model_run_id TEXT,
                failure_stage TEXT NOT NULL,
                reason TEXT NOT NULL,
                raw_model_output TEXT,
                raw_model_output_status TEXT NOT NULL CHECK(raw_model_output_status IN ('available', 'not_available')),
                data_identity TEXT NOT NULL,
                created_at TEXT NOT NULL
            );
            CREATE TABLE IF NOT EXISTS stage0_input_assembly (
                assembly_id TEXT PRIMARY KEY,
                task_id TEXT NOT NULL REFERENCES stage0_content_task(task_id),
                node TEXT NOT NULL,
                upstream_version_id TEXT,
                payload_json TEXT NOT NULL,
                integrity_hash TEXT NOT NULL,
                data_identity TEXT NOT NULL,
                created_at TEXT NOT NULL
            );
            CREATE TABLE IF NOT EXISTS stage0_model_run (
                model_run_id TEXT PRIMARY KEY,
                task_id TEXT NOT NULL REFERENCES stage0_content_task(task_id),
                node TEXT NOT NULL,
                node_version_id TEXT NOT NULL REFERENCES stage0_content_node_version(version_id),
                input_assembly_id TEXT NOT NULL REFERENCES stage0_input_assembly(assembly_id),
                status TEXT NOT NULL,
                request_id TEXT,
                prompt_version TEXT NOT NULL,
                skill_version TEXT NOT NULL,
                route_id TEXT NOT NULL,
                route_version TEXT NOT NULL,
                provider_ref TEXT,
                provider_name TEXT NOT NULL,
                model_name TEXT NOT NULL,
                input_integrity_hash TEXT NOT NULL,
                output_hash TEXT,
                output_version_id TEXT,
                validation_status TEXT NOT NULL,
                error_json TEXT,
                retry_status TEXT NOT NULL,
                usage_json TEXT NOT NULL,
                cost_json TEXT NOT NULL,
                duration_ms INTEGER NOT NULL,
                data_identity TEXT NOT NULL,
                created_at TEXT NOT NULL,
                via_model_gateway INTEGER NOT NULL CHECK(via_model_gateway = 1)
            );
            CREATE TABLE IF NOT EXISTS stage0_command_receipt (
                command_scope TEXT NOT NULL,
                idempotency_key TEXT NOT NULL,
                request_hash TEXT NOT NULL,
                result_json TEXT NOT NULL,
                created_at TEXT NOT NULL,
                PRIMARY KEY(command_scope, idempotency_key)
            );
            CREATE TABLE IF NOT EXISTS stage0_audit_event (
                audit_id TEXT PRIMARY KEY,
                task_id TEXT,
                action TEXT NOT NULL,
                payload_json TEXT NOT NULL,
                data_identity TEXT NOT NULL,
                created_at TEXT NOT NULL
            );
            CREATE TABLE IF NOT EXISTS stage1a_artifact_payload (
                version_id TEXT PRIMARY KEY REFERENCES stage0_content_node_version(version_id),
                artifact_kind TEXT NOT NULL CHECK(artifact_kind IN ('formal_topic', 'research_plan')),
                payload_json TEXT NOT NULL,
                integrity_hash TEXT NOT NULL,
                data_identity TEXT NOT NULL,
                created_at TEXT NOT NULL
            );
            CREATE TABLE IF NOT EXISTS stage0_content_artifact_payload (
                version_id TEXT PRIMARY KEY REFERENCES stage0_content_node_version(version_id),
                artifact_kind TEXT NOT NULL CHECK(artifact_kind IN ('deep_research', 'content_plan', 'formal_draft', 'copy_optimization', 'de_ai_revision', 'review')),
                payload_json TEXT NOT NULL,
                integrity_hash TEXT NOT NULL,
                data_identity TEXT NOT NULL,
                created_at TEXT NOT NULL
            );
            CREATE TABLE IF NOT EXISTS stage0_research_material (
                research_material_id TEXT PRIMARY KEY,
                task_id TEXT NOT NULL REFERENCES stage0_content_task(task_id),
                source_ref TEXT NOT NULL,
                title TEXT NOT NULL,
                evidence_role TEXT NOT NULL CHECK(evidence_role IN ('fact_evidence', 'professional_interpretation', 'audience_perception', 'research_clue')),
                material_json TEXT NOT NULL,
                integrity_hash TEXT NOT NULL,
                data_identity TEXT NOT NULL,
                collected_at TEXT NOT NULL,
                UNIQUE(task_id, source_ref)
            );
            CREATE TABLE IF NOT EXISTS stage0_manual_source (
                manual_source_id TEXT PRIMARY KEY,
                domain_label TEXT NOT NULL,
                account_ref TEXT,
                source_kind TEXT NOT NULL CHECK(source_kind IN ('direction', 'link', 'person', 'work', 'playlist')),
                original_content_json TEXT NOT NULL,
                submitted_by TEXT NOT NULL,
                submitted_at TEXT NOT NULL,
                evidence_role TEXT NOT NULL CHECK(evidence_role = 'user_origin_not_fact'),
                integrity_hash TEXT NOT NULL,
                data_identity TEXT NOT NULL
            );
            CREATE TABLE IF NOT EXISTS stage0_manual_source_link (
                link_id TEXT PRIMARY KEY,
                manual_source_id TEXT NOT NULL REFERENCES stage0_manual_source(manual_source_id),
                target_kind TEXT NOT NULL CHECK(target_kind IN ('saved_user_direction', 'candidate', 'formal_topic', 'person_exploration', 'work_exploration', 'playlist_exploration')),
                target_id TEXT NOT NULL,
                data_identity TEXT NOT NULL,
                linked_at TEXT NOT NULL,
                UNIQUE(manual_source_id, target_kind, target_id)
            );
            CREATE TABLE IF NOT EXISTS stage0_manual_exploration (
                exploration_id TEXT PRIMARY KEY,
                manual_source_id TEXT NOT NULL REFERENCES stage0_manual_source(manual_source_id),
                exploration_kind TEXT NOT NULL CHECK(exploration_kind IN ('person_exploration', 'work_exploration', 'playlist_exploration')),
                scope_json TEXT NOT NULL,
                status TEXT NOT NULL CHECK(status IN ('awaiting_material_collection', 'collecting', 'awaiting_human_direction', 'completed', 'cancelled')),
                data_identity TEXT NOT NULL,
                created_by TEXT NOT NULL,
                created_at TEXT NOT NULL,
                UNIQUE(manual_source_id, exploration_kind)
            );
            CREATE TABLE IF NOT EXISTS stage0_manual_exploration_state (
                exploration_state_id TEXT PRIMARY KEY,
                exploration_id TEXT NOT NULL REFERENCES stage0_manual_exploration(exploration_id),
                status TEXT NOT NULL CHECK(status IN ('awaiting_material_collection', 'collecting', 'awaiting_human_direction', 'completed', 'cancelled')),
                reason TEXT NOT NULL,
                data_identity TEXT NOT NULL,
                effective_at TEXT NOT NULL
            );
            CREATE TABLE IF NOT EXISTS stage0_manual_exploration_material (
                exploration_material_id TEXT PRIMARY KEY,
                exploration_id TEXT NOT NULL REFERENCES stage0_manual_exploration(exploration_id),
                material_ref_json TEXT NOT NULL,
                evidence_role TEXT NOT NULL CHECK(evidence_role IN ('fact_evidence', 'professional_interpretation', 'audience_perception', 'video_material', 'research_clue')),
                data_identity TEXT NOT NULL,
                collected_at TEXT NOT NULL,
                UNIQUE(exploration_id, material_ref_json)
            );
            CREATE TABLE IF NOT EXISTS stage0_music_audience_echo (
                audience_echo_id TEXT PRIMARY KEY,
                exploration_id TEXT NOT NULL REFERENCES stage0_manual_exploration(exploration_id),
                platform TEXT NOT NULL CHECK(platform IN ('netease_music', 'douban')),
                work_title TEXT NOT NULL,
                subject_kind TEXT NOT NULL CHECK(subject_kind IN ('song', 'album')),
                material_kind TEXT NOT NULL CHECK(material_kind IN ('comment', 'short_review', 'long_review')),
                text TEXT NOT NULL,
                useful_count INTEGER NOT NULL,
                source_url TEXT NOT NULL,
                viewed_position INTEGER NOT NULL,
                data_identity TEXT NOT NULL,
                collected_at TEXT NOT NULL,
                UNIQUE(exploration_id, platform, source_url, material_kind, viewed_position)
            );
            CREATE TABLE IF NOT EXISTS stage0_voice_profile (
                voice_profile_id TEXT PRIMARY KEY,
                profile_label TEXT NOT NULL,
                reference_audio_ref TEXT NOT NULL,
                emotion_reference_audio_ref TEXT,
                mode TEXT NOT NULL CHECK(mode IN ('basic', 'ultimate')),
                prompt_text TEXT NOT NULL,
                emotion_prompt_text TEXT NOT NULL,
                settings_json TEXT NOT NULL,
                status TEXT NOT NULL CHECK(status IN ('awaiting_human_confirmation', 'active', 'retired')),
                data_identity TEXT NOT NULL,
                submitted_by TEXT NOT NULL,
                submitted_at TEXT NOT NULL,
                confirmed_by TEXT,
                confirmed_at TEXT
            );
            CREATE TABLE IF NOT EXISTS stage0_audio_production (
                audio_production_id TEXT PRIMARY KEY,
                task_id TEXT NOT NULL REFERENCES stage0_content_task(task_id),
                approved_content_version_id TEXT NOT NULL REFERENCES stage0_content_node_version(version_id),
                voice_profile_id TEXT NOT NULL REFERENCES stage0_voice_profile(voice_profile_id),
                attempt_number INTEGER NOT NULL CHECK(attempt_number > 0),
                title TEXT NOT NULL,
                script_text_hash TEXT NOT NULL,
                status TEXT NOT NULL CHECK(status IN ('processing', 'awaiting_human_review', 'approved', 'returned', 'failed')),
                audio_ref TEXT,
                metadata_ref TEXT,
                synthesis_json TEXT NOT NULL,
                quality_review_json TEXT NOT NULL,
                failure_json TEXT NOT NULL,
                audio_delivery_id TEXT,
                data_identity TEXT NOT NULL,
                created_by TEXT NOT NULL,
                created_at TEXT NOT NULL,
                updated_at TEXT NOT NULL,
                UNIQUE(task_id, approved_content_version_id, attempt_number)
            );
            CREATE TABLE IF NOT EXISTS stage0_audio_decision (
                audio_decision_id TEXT PRIMARY KEY,
                audio_production_id TEXT NOT NULL UNIQUE REFERENCES stage0_audio_production(audio_production_id),
                decision TEXT NOT NULL CHECK(decision IN ('approved', 'returned')),
                issue_scope TEXT NOT NULL CHECK(issue_scope IN ('none', 'audio_regeneration', 'content_revision')),
                reason TEXT NOT NULL,
                actor TEXT NOT NULL,
                actor_kind TEXT NOT NULL CHECK(actor_kind='user'),
                data_identity TEXT NOT NULL,
                created_at TEXT NOT NULL
            );
            CREATE TABLE IF NOT EXISTS stage0_human_decision_carrier_binding (
                carrier_binding_id TEXT PRIMARY KEY,
                carrier_kind TEXT NOT NULL,
                entry_ref TEXT NOT NULL,
                context_strategy TEXT NOT NULL,
                status TEXT NOT NULL CHECK(status IN ('proposed', 'validated', 'retired')),
                validation_evidence_json TEXT NOT NULL,
                data_identity TEXT NOT NULL,
                proposed_by TEXT NOT NULL,
                proposed_at TEXT NOT NULL,
                validated_by TEXT,
                validated_at TEXT,
                UNIQUE(carrier_kind, entry_ref, data_identity)
            );
            CREATE TABLE IF NOT EXISTS stage0_human_decision_command (
                command_id TEXT PRIMARY KEY,
                carrier_binding_id TEXT NOT NULL REFERENCES stage0_human_decision_carrier_binding(carrier_binding_id),
                session_ref TEXT NOT NULL,
                action TEXT NOT NULL,
                target_ref TEXT NOT NULL,
                payload_json TEXT NOT NULL,
                actor TEXT NOT NULL,
                actor_kind TEXT NOT NULL CHECK(actor_kind='user'),
                status TEXT NOT NULL CHECK(status IN ('received', 'completed', 'rejected')),
                result_json TEXT NOT NULL,
                error_json TEXT NOT NULL,
                data_identity TEXT NOT NULL,
                received_at TEXT NOT NULL,
                completed_at TEXT,
                UNIQUE(carrier_binding_id, command_id, data_identity)
            );
            CREATE TABLE IF NOT EXISTS stage0_live_cold_start_preflight (
                preflight_receipt_id TEXT PRIMARY KEY,
                domain_label TEXT NOT NULL,
                owned_account_id TEXT NOT NULL,
                competitor_account_ids_json TEXT NOT NULL,
                voice_profile_id TEXT NOT NULL,
                report_json TEXT NOT NULL,
                report_hash TEXT NOT NULL,
                status TEXT NOT NULL CHECK(status IN ('ready', 'consumed')),
                consumed_by_cold_start_id TEXT,
                data_identity TEXT NOT NULL,
                inspected_at TEXT NOT NULL
            );
            CREATE TABLE IF NOT EXISTS stage0_audio_delivery (
                audio_delivery_id TEXT PRIMARY KEY,
                task_id TEXT NOT NULL REFERENCES stage0_content_task(task_id),
                approved_content_version_id TEXT NOT NULL REFERENCES stage0_content_node_version(version_id),
                audio_ref TEXT NOT NULL,
                status TEXT NOT NULL CHECK(status IN ('delivered')),
                data_identity TEXT NOT NULL,
                created_by TEXT NOT NULL,
                created_at TEXT NOT NULL,
                UNIQUE(task_id, audio_ref)
            );
            CREATE TABLE IF NOT EXISTS stage0_experience_candidate (
                experience_candidate_id TEXT PRIMARY KEY,
                task_id TEXT NOT NULL REFERENCES stage0_content_task(task_id),
                domain_label TEXT NOT NULL,
                source_fingerprint TEXT NOT NULL,
                frozen_sources_json TEXT NOT NULL,
                status TEXT NOT NULL CHECK(status IN ('preparing', 'awaiting_human_decision', 'no_proposal', 'failed', 'accepted', 'rejected')),
                proposal_json TEXT,
                failure_json TEXT NOT NULL,
                data_identity TEXT NOT NULL,
                created_by TEXT NOT NULL,
                created_at TEXT NOT NULL,
                decided_by TEXT,
                decided_at TEXT,
                decision_reason TEXT,
                UNIQUE(task_id, source_fingerprint)
            );
            CREATE TABLE IF NOT EXISTS stage0_experience_candidate_model_run (
                experience_candidate_model_run_id TEXT PRIMARY KEY,
                experience_candidate_id TEXT NOT NULL REFERENCES stage0_experience_candidate(experience_candidate_id),
                status TEXT NOT NULL CHECK(status IN ('succeeded', 'failed')),
                route_name TEXT NOT NULL,
                provider_name TEXT NOT NULL,
                model_name TEXT NOT NULL,
                input_hash TEXT NOT NULL,
                output_hash TEXT,
                envelope_json TEXT NOT NULL,
                data_identity TEXT NOT NULL,
                created_at TEXT NOT NULL
            );
            CREATE TABLE IF NOT EXISTS stage0_confirmed_experience (
                experience_id TEXT PRIMARY KEY,
                experience_candidate_id TEXT NOT NULL UNIQUE REFERENCES stage0_experience_candidate(experience_candidate_id),
                domain_label TEXT NOT NULL,
                classification TEXT NOT NULL CHECK(classification IN ('shared_pattern', 'single_source_feature')),
                summary TEXT NOT NULL,
                applicable_when_json TEXT NOT NULL,
                method_json TEXT NOT NULL,
                source_refs_json TEXT NOT NULL,
                boundary_json TEXT NOT NULL,
                status TEXT NOT NULL CHECK(status IN ('active', 'paused')),
                data_identity TEXT NOT NULL,
                confirmed_by TEXT NOT NULL,
                confirmed_at TEXT NOT NULL
            );
            CREATE TABLE IF NOT EXISTS stage0_unregistered_account_video (
                account_video_observation_id TEXT PRIMARY KEY,
                domain_label TEXT NOT NULL,
                account_ref TEXT NOT NULL,
                account_display_name TEXT NOT NULL,
                video_ref TEXT NOT NULL,
                video_url TEXT NOT NULL,
                video_title TEXT NOT NULL,
                qualified INTEGER NOT NULL CHECK(qualified IN (0, 1)),
                source_ref_json TEXT NOT NULL,
                observed_at TEXT NOT NULL,
                data_identity TEXT NOT NULL,
                UNIQUE(domain_label, account_ref, video_ref)
            );
            CREATE TABLE IF NOT EXISTS stage0_unregistered_account_review (
                account_review_id TEXT PRIMARY KEY,
                domain_label TEXT NOT NULL,
                account_ref TEXT NOT NULL,
                account_display_name TEXT NOT NULL,
                triggering_video_refs_json TEXT NOT NULL,
                recent_accessible_videos_json TEXT NOT NULL,
                status TEXT NOT NULL CHECK(status IN ('awaiting_human_review', 'accepted', 'rejected')),
                data_identity TEXT NOT NULL,
                created_at TEXT NOT NULL,
                decided_by TEXT,
                decided_at TEXT,
                decision_reason TEXT,
                UNIQUE(domain_label, account_ref, status)
            );
            CREATE TABLE IF NOT EXISTS stage0_content_account (
                content_account_id TEXT PRIMARY KEY,
                account_role TEXT NOT NULL CHECK(account_role IN ('owned', 'competitor')),
                display_name TEXT NOT NULL,
                domain_label TEXT NOT NULL,
                external_account_ref TEXT,
                status TEXT NOT NULL CHECK(status IN ('active', 'disabled')),
                data_identity TEXT NOT NULL,
                created_by TEXT NOT NULL,
                created_at TEXT NOT NULL,
                UNIQUE(account_role, domain_label, external_account_ref)
            );
            CREATE TABLE IF NOT EXISTS stage0_cold_start_configuration (
                configuration_id TEXT PRIMARY KEY,
                confirmation_key TEXT NOT NULL,
                domain_mode TEXT NOT NULL CHECK(domain_mode IN ('reuse', 'create')),
                domain_label TEXT NOT NULL,
                domain_name TEXT NOT NULL,
                domain_boundary TEXT NOT NULL,
                platform TEXT NOT NULL,
                owned_account_id TEXT NOT NULL REFERENCES stage0_content_account(content_account_id),
                competitor_account_ids_json TEXT NOT NULL,
                status TEXT NOT NULL CHECK(status IN ('confirmed', 'started', 'completed', 'cancelled')),
                data_identity TEXT NOT NULL,
                confirmed_by TEXT NOT NULL,
                confirmed_at TEXT NOT NULL,
                cold_start_id TEXT,
                UNIQUE(confirmation_key, data_identity)
            );
            CREATE TABLE IF NOT EXISTS stage0_cold_start (
                cold_start_id TEXT PRIMARY KEY,
                owned_account_id TEXT NOT NULL REFERENCES stage0_content_account(content_account_id),
                domain_label TEXT NOT NULL,
                status TEXT NOT NULL CHECK(status IN ('registering_competitors', 'awaiting_human_review', 'completed', 'cancelled')),
                data_identity TEXT NOT NULL,
                created_by TEXT NOT NULL,
                created_at TEXT NOT NULL,
                completed_at TEXT,
                UNIQUE(owned_account_id, domain_label, data_identity)
            );
            CREATE TABLE IF NOT EXISTS stage0_competitor_registration (
                registration_id TEXT PRIMARY KEY,
                cold_start_id TEXT NOT NULL REFERENCES stage0_cold_start(cold_start_id),
                competitor_account_id TEXT NOT NULL REFERENCES stage0_content_account(content_account_id),
                current_step TEXT NOT NULL CHECK(current_step IN ('historical_material', 'high_signal_identification', 'transcripts_and_comments', 'breakdown', 'tag_candidates', 'domain_summary', 'awaiting_human_review', 'completed', 'failed')),
                status TEXT NOT NULL CHECK(status IN ('processing', 'awaiting_human_review', 'completed', 'failed')),
                data_identity TEXT NOT NULL,
                created_at TEXT NOT NULL,
                completed_at TEXT,
                UNIQUE(cold_start_id, competitor_account_id)
            );
            CREATE TABLE IF NOT EXISTS stage0_competitor_registration_step (
                step_record_id TEXT PRIMARY KEY,
                registration_id TEXT NOT NULL REFERENCES stage0_competitor_registration(registration_id),
                step_name TEXT NOT NULL CHECK(step_name IN ('historical_material', 'high_signal_identification', 'transcripts_and_comments', 'breakdown', 'tag_candidates', 'domain_summary')),
                artifact_refs_json TEXT NOT NULL,
                integrity_hash TEXT NOT NULL,
                completed_by TEXT NOT NULL,
                completed_at TEXT NOT NULL,
                data_identity TEXT NOT NULL,
                UNIQUE(registration_id, step_name)
            );
            CREATE TABLE IF NOT EXISTS stage0_competitor_registration_model_run (
                registration_model_run_id TEXT PRIMARY KEY,
                registration_id TEXT NOT NULL REFERENCES stage0_competitor_registration(registration_id),
                step_name TEXT NOT NULL CHECK(step_name IN ('breakdown', 'tag_candidates')),
                status TEXT NOT NULL,
                route_name TEXT NOT NULL,
                provider_name TEXT NOT NULL,
                model_name TEXT NOT NULL,
                input_hash TEXT NOT NULL,
                output_hash TEXT,
                error_json TEXT NOT NULL,
                usage_json TEXT NOT NULL,
                cost_json TEXT NOT NULL,
                duration_ms INTEGER NOT NULL,
                data_identity TEXT NOT NULL,
                created_at TEXT NOT NULL
            );
            CREATE TABLE IF NOT EXISTS stage0_daily_hit_model_run (
                daily_hit_model_run_id TEXT PRIMARY KEY,
                hit_id TEXT NOT NULL REFERENCES hits(hit_id),
                status TEXT NOT NULL,
                route_name TEXT NOT NULL,
                provider_name TEXT NOT NULL,
                model_name TEXT NOT NULL,
                input_hash TEXT NOT NULL,
                output_hash TEXT,
                error_json TEXT NOT NULL,
                usage_json TEXT NOT NULL,
                cost_json TEXT NOT NULL,
                duration_ms INTEGER NOT NULL,
                data_identity TEXT NOT NULL,
                created_at TEXT NOT NULL
            );
            CREATE TABLE IF NOT EXISTS stage0_competitor_registration_item (
                registration_id TEXT NOT NULL REFERENCES stage0_competitor_registration(registration_id),
                step_name TEXT NOT NULL CHECK(step_name IN ('transcripts_and_comments', 'breakdown')),
                item_ref TEXT NOT NULL,
                status TEXT NOT NULL CHECK(status IN ('completed', 'failed', 'excluded')),
                artifact_json TEXT NOT NULL,
                error_json TEXT NOT NULL,
                attempt_count INTEGER NOT NULL,
                data_identity TEXT NOT NULL,
                updated_at TEXT NOT NULL,
                PRIMARY KEY(registration_id, step_name, item_ref)
            );
            CREATE TABLE IF NOT EXISTS stage0_competitor_breakdown_attempt (
                breakdown_attempt_id TEXT PRIMARY KEY,
                registration_id TEXT NOT NULL REFERENCES stage0_competitor_registration(registration_id),
                source_id TEXT NOT NULL,
                attempt_kind TEXT NOT NULL CHECK(attempt_kind IN ('initial', 'post_batch_delivery_retry')),
                outcome TEXT NOT NULL CHECK(outcome IN ('completed', 'delivery_interrupted', 'failed')),
                reason TEXT NOT NULL,
                raw_model_output TEXT,
                raw_model_output_status TEXT NOT NULL,
                model_run_id TEXT,
                data_identity TEXT NOT NULL,
                created_at TEXT NOT NULL
            );
            CREATE INDEX IF NOT EXISTS stage0_competitor_breakdown_attempt_source_idx
            ON stage0_competitor_breakdown_attempt(registration_id, source_id, data_identity, created_at);
            CREATE TABLE IF NOT EXISTS stage0_competitor_breakdown_backlog_task (
                backlog_task_id TEXT PRIMARY KEY,
                cold_start_id TEXT NOT NULL REFERENCES stage0_cold_start(cold_start_id),
                status TEXT NOT NULL CHECK(status IN ('queued', 'running', 'completed', 'completed_with_failures')),
                source_snapshot_json TEXT NOT NULL,
                approval_json TEXT NOT NULL,
                summary_json TEXT NOT NULL,
                data_identity TEXT NOT NULL,
                created_at TEXT NOT NULL,
                updated_at TEXT NOT NULL,
                completed_at TEXT
            );
            CREATE INDEX IF NOT EXISTS stage0_competitor_breakdown_backlog_task_active_idx
            ON stage0_competitor_breakdown_backlog_task(cold_start_id, data_identity, status, created_at);
            CREATE TABLE IF NOT EXISTS stage0_competitor_material_collection_checkpoint (
                registration_id TEXT NOT NULL REFERENCES stage0_competitor_registration(registration_id),
                item_ref TEXT NOT NULL,
                detail_json TEXT NOT NULL,
                comments_json TEXT NOT NULL,
                collection_ref TEXT NOT NULL,
                data_identity TEXT NOT NULL,
                recorded_at TEXT NOT NULL,
                PRIMARY KEY(registration_id, item_ref)
            );
            CREATE TABLE IF NOT EXISTS stage0_cold_start_tag_library (
                tag_library_id TEXT PRIMARY KEY,
                cold_start_id TEXT NOT NULL UNIQUE REFERENCES stage0_cold_start(cold_start_id),
                domain_label TEXT NOT NULL,
                extraction_method TEXT NOT NULL,
                source_item_count INTEGER NOT NULL,
                minimum_source_support INTEGER NOT NULL,
                candidate_set_json TEXT NOT NULL,
                tag_ids_json TEXT NOT NULL,
                status TEXT NOT NULL CHECK(status IN ('awaiting_human_review', 'accepted', 'rejected')),
                data_identity TEXT NOT NULL,
                created_by TEXT NOT NULL,
                created_at TEXT NOT NULL,
                reviewed_by TEXT,
                reviewed_at TEXT,
                review_reason TEXT
            );
            CREATE TABLE IF NOT EXISTS stage0_two_week_tag_library_review (
                tag_review_id TEXT PRIMARY KEY,
                domain_label TEXT NOT NULL,
                window_start TEXT NOT NULL,
                window_end TEXT NOT NULL,
                candidate_set_json TEXT NOT NULL,
                tag_ids_json TEXT NOT NULL,
                status TEXT NOT NULL CHECK(status IN ('awaiting_human_review', 'accepted', 'rejected')),
                data_identity TEXT NOT NULL,
                created_by TEXT NOT NULL,
                created_at TEXT NOT NULL,
                reviewed_by TEXT,
                reviewed_at TEXT,
                review_reason TEXT,
                UNIQUE(domain_label, window_start, window_end, data_identity)
            );
            CREATE TABLE IF NOT EXISTS stage0_knowledge_mirror_run (
                mirror_run_id TEXT PRIMARY KEY,
                cold_start_id TEXT REFERENCES stage0_cold_start(cold_start_id),
                mirror_root TEXT NOT NULL,
                record_count INTEGER NOT NULL,
                relationship_count INTEGER NOT NULL,
                status TEXT NOT NULL CHECK(status IN ('completed', 'failed')),
                data_identity TEXT NOT NULL,
                completed_by TEXT NOT NULL,
                completed_at TEXT NOT NULL
            );
            CREATE TABLE IF NOT EXISTS stage0_cold_start_domain_summary (
                cold_start_id TEXT PRIMARY KEY REFERENCES stage0_cold_start(cold_start_id),
                artifact_refs_json TEXT NOT NULL,
                integrity_hash TEXT NOT NULL,
                completed_by TEXT NOT NULL,
                completed_at TEXT NOT NULL,
                data_identity TEXT NOT NULL
            );
            CREATE TRIGGER IF NOT EXISTS stage0_competitor_registration_step_immutable_update
            BEFORE UPDATE ON stage0_competitor_registration_step
            BEGIN SELECT RAISE(ABORT, 'competitor registration steps are immutable'); END;
            CREATE TRIGGER IF NOT EXISTS stage0_competitor_registration_step_immutable_delete
            BEFORE DELETE ON stage0_competitor_registration_step
            BEGIN SELECT RAISE(ABORT, 'competitor registration steps are immutable'); END;
            CREATE TRIGGER IF NOT EXISTS stage0_manual_source_immutable_update
            BEFORE UPDATE ON stage0_manual_source
            BEGIN SELECT RAISE(ABORT, 'manual sources are immutable'); END;
            CREATE TRIGGER IF NOT EXISTS stage0_manual_source_immutable_delete
            BEFORE DELETE ON stage0_manual_source
            BEGIN SELECT RAISE(ABORT, 'manual sources are immutable'); END;
            CREATE TRIGGER IF NOT EXISTS stage0_manual_source_link_immutable_update
            BEFORE UPDATE ON stage0_manual_source_link
            BEGIN SELECT RAISE(ABORT, 'manual source links are immutable'); END;
            CREATE TRIGGER IF NOT EXISTS stage0_manual_source_link_immutable_delete
            BEFORE DELETE ON stage0_manual_source_link
            BEGIN SELECT RAISE(ABORT, 'manual source links are immutable'); END;
            CREATE TRIGGER IF NOT EXISTS stage0_manual_exploration_immutable_update
            BEFORE UPDATE ON stage0_manual_exploration
            BEGIN SELECT RAISE(ABORT, 'manual explorations are immutable; append the next formal state instead'); END;
            CREATE TRIGGER IF NOT EXISTS stage0_manual_exploration_immutable_delete
            BEFORE DELETE ON stage0_manual_exploration
            BEGIN SELECT RAISE(ABORT, 'manual explorations are immutable'); END;
            CREATE TRIGGER IF NOT EXISTS stage0_node_version_immutable_update
            BEFORE UPDATE ON stage0_content_node_version
            BEGIN SELECT RAISE(ABORT, 'stage0 node versions are immutable'); END;
            CREATE TRIGGER IF NOT EXISTS stage0_node_version_immutable_delete
            BEFORE DELETE ON stage0_content_node_version
            BEGIN SELECT RAISE(ABORT, 'stage0 node versions are immutable'); END;
            CREATE TRIGGER IF NOT EXISTS stage0_node_failure_immutable_update
            BEFORE UPDATE ON stage0_content_node_failure
            BEGIN SELECT RAISE(ABORT, 'stage0 node failures are immutable'); END;
            CREATE TRIGGER IF NOT EXISTS stage0_node_failure_immutable_delete
            BEFORE DELETE ON stage0_content_node_failure
            BEGIN SELECT RAISE(ABORT, 'stage0 node failures are immutable'); END;
            CREATE TRIGGER IF NOT EXISTS stage1a_artifact_payload_immutable_update
            BEFORE UPDATE ON stage1a_artifact_payload
            BEGIN SELECT RAISE(ABORT, 'stage1a artifact payloads are immutable'); END;
            CREATE TRIGGER IF NOT EXISTS stage1a_artifact_payload_immutable_delete
            BEFORE DELETE ON stage1a_artifact_payload
            BEGIN SELECT RAISE(ABORT, 'stage1a artifact payloads are immutable'); END;
            CREATE TABLE IF NOT EXISTS stage1b_discovery_run (
                run_id TEXT PRIMARY KEY,
                discovery_date TEXT NOT NULL,
                status TEXT NOT NULL CHECK(status IN ('processing', 'completed', 'failed', 'cancelled')),
                data_identity TEXT NOT NULL,
                created_by TEXT NOT NULL,
                created_at TEXT NOT NULL,
                completed_at TEXT,
                failure_reason TEXT
            );
            CREATE TABLE IF NOT EXISTS stage1b_run_execution_context (
                run_id TEXT PRIMARY KEY REFERENCES stage1b_discovery_run(run_id),
                execution_mode TEXT NOT NULL CHECK(execution_mode IN ('test_isolated', 'real_daily_validation', 'production_daily', 'retired_legacy_validation')),
                lifecycle_status TEXT NOT NULL CHECK(lifecycle_status IN ('processing', 'completed', 'completed_with_failures', 'timed_out', 'interrupted', 'failed', 'cancelled')),
                classification_reason TEXT NOT NULL,
                classified_by TEXT NOT NULL,
                classified_at TEXT NOT NULL,
                data_identity TEXT NOT NULL
            );
            CREATE TABLE IF NOT EXISTS stage1b_run_domain_scope (
                run_id TEXT NOT NULL REFERENCES stage1b_discovery_run(run_id),
                domain_label TEXT NOT NULL,
                data_identity TEXT NOT NULL,
                recorded_at TEXT NOT NULL,
                PRIMARY KEY(run_id, domain_label)
            );
            CREATE TABLE IF NOT EXISTS stage1_question_expansion_source (
                expansion_id TEXT PRIMARY KEY,
                domain_label TEXT NOT NULL,
                core_question TEXT NOT NULL,
                parent_source_ref_json TEXT NOT NULL,
                validation_outcome TEXT NOT NULL CHECK(validation_outcome = 'supported'),
                validated_at TEXT NOT NULL,
                payload_json TEXT NOT NULL,
                integrity_hash TEXT NOT NULL,
                data_identity TEXT NOT NULL,
                created_by TEXT NOT NULL
            );
            CREATE TABLE IF NOT EXISTS stage1_saved_user_direction_source (
                direction_id TEXT PRIMARY KEY,
                domain_label TEXT NOT NULL,
                core_question TEXT NOT NULL,
                submitted_by TEXT NOT NULL,
                saved_at TEXT NOT NULL,
                payload_json TEXT NOT NULL,
                integrity_hash TEXT NOT NULL,
                status TEXT NOT NULL CHECK(status IN ('active', 'closed')),
                data_identity TEXT NOT NULL
            );
            CREATE TABLE IF NOT EXISTS stage1b_source_version (
                source_version_id TEXT PRIMARY KEY,
                run_id TEXT NOT NULL REFERENCES stage1b_discovery_run(run_id),
                domain_label TEXT NOT NULL,
                source_type TEXT NOT NULL CHECK(source_type IN ('daily_competitor_content', 'historical_high_signal', 'hotspot', 'tag_discovery', 'question_expansion', 'saved_user_direction')),
                source_object_id TEXT NOT NULL,
                source_object_version TEXT NOT NULL,
                source_time TEXT NOT NULL,
                expires_at TEXT,
                payload_json TEXT NOT NULL,
                integrity_hash TEXT NOT NULL,
                data_identity TEXT NOT NULL,
                created_at TEXT NOT NULL,
                UNIQUE(run_id, source_type, source_object_id, source_object_version)
            );
            CREATE TABLE IF NOT EXISTS stage1b_filter_result (
                filter_result_id TEXT PRIMARY KEY,
                source_version_id TEXT NOT NULL REFERENCES stage1b_source_version(source_version_id),
                outcome TEXT NOT NULL CHECK(outcome IN ('eligible', 'excluded')),
                reason_code TEXT NOT NULL,
                detail_json TEXT NOT NULL,
                data_identity TEXT NOT NULL,
                created_at TEXT NOT NULL,
                UNIQUE(source_version_id)
            );
            CREATE TABLE IF NOT EXISTS stage1b_input_assembly (
                assembly_id TEXT PRIMARY KEY,
                run_id TEXT NOT NULL REFERENCES stage1b_discovery_run(run_id),
                source_version_id TEXT NOT NULL REFERENCES stage1b_source_version(source_version_id),
                payload_json TEXT NOT NULL,
                integrity_hash TEXT NOT NULL,
                prompt_version TEXT NOT NULL,
                skill_version TEXT NOT NULL,
                model_config_version TEXT NOT NULL,
                data_identity TEXT NOT NULL,
                created_at TEXT NOT NULL,
                UNIQUE(run_id, source_version_id)
            );
            CREATE TABLE IF NOT EXISTS stage1b_model_run (
                model_run_id TEXT PRIMARY KEY,
                run_id TEXT NOT NULL REFERENCES stage1b_discovery_run(run_id),
                source_version_id TEXT NOT NULL REFERENCES stage1b_source_version(source_version_id),
                input_assembly_id TEXT NOT NULL REFERENCES stage1b_input_assembly(assembly_id),
                status TEXT NOT NULL,
                request_id TEXT,
                prompt_version TEXT NOT NULL,
                skill_version TEXT NOT NULL,
                route_id TEXT,
                route_version TEXT NOT NULL,
                provider_ref TEXT,
                provider_name TEXT NOT NULL,
                model_name TEXT NOT NULL,
                input_integrity_hash TEXT NOT NULL,
                output_hash TEXT,
                validation_status TEXT NOT NULL,
                error_json TEXT NOT NULL,
                retry_status TEXT NOT NULL,
                usage_json TEXT NOT NULL,
                cost_json TEXT NOT NULL,
                duration_ms INTEGER NOT NULL,
                data_identity TEXT NOT NULL,
                created_at TEXT NOT NULL,
                via_model_gateway INTEGER NOT NULL CHECK(via_model_gateway IN (0, 1)),
                UNIQUE(run_id, source_version_id, input_assembly_id)
            );
            CREATE TABLE IF NOT EXISTS stage1b_candidate_version (
                candidate_version_id TEXT PRIMARY KEY,
                candidate_id TEXT NOT NULL,
                run_id TEXT NOT NULL REFERENCES stage1b_discovery_run(run_id),
                domain_label TEXT NOT NULL,
                source_version_id TEXT NOT NULL REFERENCES stage1b_source_version(source_version_id),
                parent_candidate_version_id TEXT,
                model_run_id TEXT NOT NULL REFERENCES stage1b_model_run(model_run_id),
                payload_json TEXT NOT NULL,
                integrity_hash TEXT NOT NULL,
                status TEXT NOT NULL CHECK(status IN ('awaiting_user_decision', 'selected', 'deferred', 'rejected', 'evergreen', 'angle_change_requested')),
                data_identity TEXT NOT NULL,
                created_at TEXT NOT NULL,
                UNIQUE(run_id, source_version_id)
            );
            CREATE TABLE IF NOT EXISTS stage1b_candidate_assessment (
                assessment_id TEXT PRIMARY KEY,
                candidate_version_id TEXT NOT NULL REFERENCES stage1b_candidate_version(candidate_version_id),
                dimension_scores_json TEXT NOT NULL,
                dimension_reasons_json TEXT NOT NULL,
                total_score REAL NOT NULL,
                assessed_by TEXT NOT NULL,
                data_identity TEXT NOT NULL,
                assessed_at TEXT NOT NULL,
                UNIQUE(candidate_version_id)
            );
            CREATE TABLE IF NOT EXISTS stage1b_candidate_assessment_revision (
                assessment_revision_id TEXT PRIMARY KEY,
                candidate_version_id TEXT NOT NULL REFERENCES stage1b_candidate_version(candidate_version_id),
                dimension_scores_json TEXT NOT NULL,
                dimension_reasons_json TEXT NOT NULL,
                total_score REAL NOT NULL,
                assessed_by TEXT NOT NULL,
                data_identity TEXT NOT NULL,
                assessed_at TEXT NOT NULL
            );
            CREATE TABLE IF NOT EXISTS stage1b_candidate_support (
                support_id TEXT PRIMARY KEY,
                candidate_version_id TEXT NOT NULL REFERENCES stage1b_candidate_version(candidate_version_id),
                source_version_id TEXT NOT NULL REFERENCES stage1b_source_version(source_version_id),
                support_kind TEXT NOT NULL CHECK(support_kind IN ('initial_discovery', 'same_topic_same_angle_material')),
                relation_reason TEXT NOT NULL,
                data_identity TEXT NOT NULL,
                created_at TEXT NOT NULL,
                UNIQUE(candidate_version_id, source_version_id)
            );
            CREATE TABLE IF NOT EXISTS stage1b_candidate_pool_state (
                pool_state_id TEXT PRIMARY KEY,
                candidate_version_id TEXT NOT NULL REFERENCES stage1b_candidate_version(candidate_version_id),
                pool_status TEXT NOT NULL CHECK(pool_status IN ('current', 'historical')),
                reason TEXT NOT NULL,
                parameter_version TEXT NOT NULL,
                data_identity TEXT NOT NULL,
                effective_at TEXT NOT NULL
            );
            CREATE TABLE IF NOT EXISTS stage1b_candidate_pool_parameter_version (
                parameter_version TEXT PRIMARY KEY,
                observation_window_days INTEGER NOT NULL CHECK(observation_window_days > 0),
                changed_by TEXT NOT NULL,
                change_reason TEXT NOT NULL,
                data_identity TEXT NOT NULL,
                effective_at TEXT NOT NULL
            );
            CREATE TABLE IF NOT EXISTS stage1b_candidate_relation (
                relation_id TEXT PRIMARY KEY,
                candidate_version_id TEXT NOT NULL REFERENCES stage1b_candidate_version(candidate_version_id),
                related_candidate_version_id TEXT NOT NULL REFERENCES stage1b_candidate_version(candidate_version_id),
                relation_kind TEXT NOT NULL CHECK(relation_kind IN ('same_topic_same_angle', 'same_topic_different_angle')),
                relation_reason TEXT NOT NULL,
                data_identity TEXT NOT NULL,
                created_at TEXT NOT NULL,
                UNIQUE(candidate_version_id, related_candidate_version_id),
                CHECK(candidate_version_id <> related_candidate_version_id)
            );
            CREATE TABLE IF NOT EXISTS stage1b_daily_snapshot (
                snapshot_id TEXT PRIMARY KEY,
                run_id TEXT NOT NULL REFERENCES stage1b_discovery_run(run_id),
                domain_label TEXT NOT NULL,
                candidate_version_id TEXT REFERENCES stage1b_candidate_version(candidate_version_id),
                display_position INTEGER NOT NULL,
                data_identity TEXT NOT NULL,
                created_at TEXT NOT NULL,
                UNIQUE(run_id, domain_label, display_position)
            );
            CREATE TABLE IF NOT EXISTS stage1b_candidate_decision (
                decision_id TEXT PRIMARY KEY,
                candidate_version_id TEXT NOT NULL REFERENCES stage1b_candidate_version(candidate_version_id),
                decision TEXT NOT NULL CHECK(decision IN ('selected', 'deferred', 'rejected', 'angle_change_requested', 'evergreen')),
                actor TEXT NOT NULL,
                reason TEXT NOT NULL,
                formal_topic_task_id TEXT,
                data_identity TEXT NOT NULL,
                created_at TEXT NOT NULL
            );
            CREATE TABLE IF NOT EXISTS stage1b_candidate_absence (
                absence_id TEXT PRIMARY KEY,
                run_id TEXT NOT NULL REFERENCES stage1b_discovery_run(run_id),
                source_version_id TEXT NOT NULL REFERENCES stage1b_source_version(source_version_id),
                model_run_id TEXT REFERENCES stage1b_model_run(model_run_id),
                reason_code TEXT NOT NULL,
                detail_json TEXT NOT NULL,
                data_identity TEXT NOT NULL,
                created_at TEXT NOT NULL,
                UNIQUE(run_id, source_version_id)
            );
            CREATE TABLE IF NOT EXISTS stage1b_source_failure (
                failure_id TEXT PRIMARY KEY,
                run_id TEXT NOT NULL REFERENCES stage1b_discovery_run(run_id),
                source_version_id TEXT NOT NULL REFERENCES stage1b_source_version(source_version_id),
                model_run_id TEXT REFERENCES stage1b_model_run(model_run_id),
                failure_stage TEXT NOT NULL,
                reason TEXT NOT NULL,
                raw_model_output TEXT,
                raw_model_output_status TEXT NOT NULL CHECK(raw_model_output_status IN ('available', 'not_available')),
                data_identity TEXT NOT NULL,
                created_at TEXT NOT NULL,
                UNIQUE(run_id, source_version_id)
            );
            CREATE TRIGGER IF NOT EXISTS stage1b_source_version_immutable_update
            BEFORE UPDATE ON stage1b_source_version
            BEGIN SELECT RAISE(ABORT, 'stage1b source versions are immutable'); END;
            CREATE TRIGGER IF NOT EXISTS stage1b_source_version_immutable_delete
            BEFORE DELETE ON stage1b_source_version
            BEGIN SELECT RAISE(ABORT, 'stage1b source versions are immutable'); END;
            CREATE TRIGGER IF NOT EXISTS stage1b_candidate_version_immutable_update
            BEFORE UPDATE ON stage1b_candidate_version
            BEGIN SELECT RAISE(ABORT, 'stage1b candidate versions are immutable'); END;
            CREATE TRIGGER IF NOT EXISTS stage1b_candidate_version_immutable_delete
            BEFORE DELETE ON stage1b_candidate_version
            BEGIN SELECT RAISE(ABORT, 'stage1b candidate versions are immutable'); END;
            CREATE TRIGGER IF NOT EXISTS stage1b_source_failure_immutable_update
            BEFORE UPDATE ON stage1b_source_failure
            BEGIN SELECT RAISE(ABORT, 'stage1b source failures are immutable'); END;
            CREATE TRIGGER IF NOT EXISTS stage1b_source_failure_immutable_delete
            BEFORE DELETE ON stage1b_source_failure
            BEGIN SELECT RAISE(ABORT, 'stage1b source failures are immutable'); END;
            CREATE TRIGGER IF NOT EXISTS stage1b_run_execution_context_mode_immutable
            BEFORE UPDATE OF execution_mode ON stage1b_run_execution_context
            BEGIN SELECT RAISE(ABORT, 'stage1b execution mode is immutable; create a new run instead'); END;
            CREATE TRIGGER IF NOT EXISTS stage1b_run_domain_scope_immutable_update
            BEFORE UPDATE ON stage1b_run_domain_scope
            BEGIN SELECT RAISE(ABORT, 'stage1b run domain scope is immutable'); END;
            CREATE TRIGGER IF NOT EXISTS stage1b_run_domain_scope_immutable_delete
            BEFORE DELETE ON stage1b_run_domain_scope
            BEGIN SELECT RAISE(ABORT, 'stage1b run domain scope is immutable'); END;
            """
        )
        competitor_schema = Path(__file__).resolve().parents[1] / "business_data" / "competitor_accounts_schema.sqlite.sql"
        self.conn.executescript(competitor_schema.read_text(encoding="utf-8"))
        discovery_schema = Path(__file__).resolve().parents[1] / "business_data" / "domain_search_schema.sqlite.sql"
        self.conn.executescript(discovery_schema.read_text(encoding="utf-8"))
        self.conn.execute("DROP TABLE IF EXISTS stage1b_candidate_cooldown")
        self.conn.execute(
            "INSERT OR IGNORE INTO stage1b_candidate_pool_parameter_version VALUES (?, ?, ?, ?, ?, ?)",
            ("candidate_pool_window_v1", 14, "system", "initial confirmed two-week observation window", self.data_identity, _now()),
        )
        self.conn.execute(
            "INSERT OR IGNORE INTO stage1b_run_domain_scope "
            "(run_id, domain_label, data_identity, recorded_at) "
            "SELECT run_id, domain_label, data_identity, MIN(created_at) "
            "FROM stage1b_source_version "
            "GROUP BY run_id, domain_label, data_identity"
        )
        self.conn.execute(
            "INSERT OR IGNORE INTO stage1b_run_domain_scope "
            "(run_id, domain_label, data_identity, recorded_at) "
            "SELECT run_id, domain_label, data_identity, MIN(created_at) "
            "FROM stage1b_daily_snapshot "
            "GROUP BY run_id, domain_label, data_identity"
        )
        from scripts.core.production.publication_feedback import install_schema as install_publication_feedback_schema
        install_publication_feedback_schema(self.conn)
        # The former per-material trigger route has been retired.  Removing a
        # leftover table here makes an old process or an older database layout
        # unable to revive that route after the batch boundary was introduced.
        self.conn.execute("DROP TABLE IF EXISTS stage0_deep_breakdown_trigger")
        self._migrate_competitor_registration_item_statuses()
        self.conn.commit()

    def _migrate_competitor_registration_item_statuses(self) -> None:
        """Allow a final, non-retry exclusion without mislabeling it as a model failure."""
        row = self.conn.execute(
            "SELECT sql FROM sqlite_master WHERE type='table' AND name='stage0_competitor_registration_item'"
        ).fetchone()
        schema_sql = str(row["sql"] or "") if row is not None else ""
        if "'excluded'" in schema_sql:
            return
        with self.conn:
            self.conn.execute("ALTER TABLE stage0_competitor_registration_item RENAME TO stage0_competitor_registration_item_legacy")
            self.conn.execute(
                "CREATE TABLE stage0_competitor_registration_item ("
                "registration_id TEXT NOT NULL REFERENCES stage0_competitor_registration(registration_id), "
                "step_name TEXT NOT NULL CHECK(step_name IN ('transcripts_and_comments', 'breakdown')), "
                "item_ref TEXT NOT NULL, "
                "status TEXT NOT NULL CHECK(status IN ('completed', 'failed', 'excluded')), "
                "artifact_json TEXT NOT NULL, error_json TEXT NOT NULL, attempt_count INTEGER NOT NULL, "
                "data_identity TEXT NOT NULL, updated_at TEXT NOT NULL, "
                "PRIMARY KEY(registration_id, step_name, item_ref))"
            )
            self.conn.execute(
                "INSERT INTO stage0_competitor_registration_item "
                "SELECT registration_id, step_name, item_ref, status, artifact_json, error_json, "
                "attempt_count, data_identity, updated_at FROM stage0_competitor_registration_item_legacy"
            )
            self.conn.execute("DROP TABLE stage0_competitor_registration_item_legacy")

    def create_task(
        self,
        *,
        topic_payload: dict[str, Any],
        actor: str,
        actor_kind: str,
        reason: str,
        idempotency_key: str,
    ) -> dict[str, str]:
        if self.data_identity == "production":
            raise StateTransitionError("production formal topics must be submitted and explicitly confirmed separately")
        if actor_kind != "user":
            raise StateTransitionError("formal topic creation requires a user confirmation")
        request = {"topic_payload": topic_payload, "actor": actor, "actor_kind": actor_kind, "reason": reason}
        replay = self._replay("create_task", idempotency_key, request)
        if replay:
            return replay
        task_id, topic_version_id = _id("task"), _id("version")
        now = _now()
        with self.conn:
            self.conn.execute(
                "INSERT INTO stage0_content_task VALUES (?, ?, ?, NULL, ?, 0, ?, ?, ?, NULL)",
                (task_id, topic_version_id, "research_plan", "not_started", self.data_identity, actor, now),
            )
            self.conn.execute(
                "INSERT INTO stage0_content_node_version VALUES (?, ?, ?, NULL, NULL, NULL, ?, NULL, ?, ?, ?, ?, ?)",
                (topic_version_id, task_id, "formal_topic", "approved", "topic_confirmed", self.data_identity, actor, now, 0),
            )
            self._decision(task_id, "formal_topic", topic_version_id, "approved", actor, actor_kind, reason)
            result = {"task_id": task_id, "topic_version_id": topic_version_id}
            self._receipt("create_task", idempotency_key, request, result)
            self._audit(task_id, "task_created", result)
        return result

    def _require_configured_domain(self, domain_label: str, *, context: str) -> str:
        normalized = str(domain_label or "").strip()
        if normalized not in formal_domain_labels():
            raise StateTransitionError(f"{context} requires a configured formal domain")
        return normalized

    def _require_owned_account_for_domain(self, *, domain_label: str, account_ref: str) -> None:
        row = self.conn.execute(
            "SELECT 1 FROM stage0_content_account "
            "WHERE account_role='owned' AND domain_label=? AND external_account_ref=? "
            "AND data_identity=? LIMIT 1",
            (domain_label, account_ref, self.data_identity),
        ).fetchone()
        if row is None:
            raise StateTransitionError("the service account does not belong to the requested domain")

    def submit_formal_topic(
        self,
        *,
        topic_payload: dict[str, Any],
        actor: str,
        actor_kind: str,
        reason: str,
        idempotency_key: str,
    ) -> dict[str, str]:
        """Create a user-supplied formal-topic version, awaiting confirmation."""
        if actor_kind != "user":
            raise StateTransitionError("formal topic submission requires a user actor")
        topic_domain = self._require_configured_domain(
            str(topic_payload.get("domain_label") or topic_payload.get("domain") or ""),
            context="formal topic submission",
        )
        topic_payload = {**topic_payload, "domain": topic_domain}
        request = {"topic_payload": topic_payload, "actor": actor, "actor_kind": actor_kind, "reason": reason}
        replay = self._replay("submit_formal_topic", idempotency_key, request)
        if replay:
            return replay
        task_id, topic_version_id = _id("task"), _id("version")
        now = _now()
        with self.conn:
            self.conn.execute(
                "INSERT INTO stage0_content_task VALUES (?, ?, ?, ?, ?, 0, ?, ?, ?, NULL)",
                (task_id, topic_version_id, "formal_topic", topic_version_id, "awaiting_human_review", self.data_identity, actor, now),
            )
            self.conn.execute(
                "INSERT INTO stage0_content_node_version VALUES (?, ?, ?, NULL, NULL, NULL, ?, ?, ?, ?, ?, ?, ?)",
                (
                    topic_version_id,
                    task_id,
                    "formal_topic",
                    "awaiting_human_review",
                    "formal_topic_payload",
                    "topic_submitted",
                    self.data_identity,
                    actor,
                    now,
                    0,
                ),
            )
            self._insert_artifact_payload(topic_version_id, "formal_topic", topic_payload)
            result = {"task_id": task_id, "topic_version_id": topic_version_id, "task_revision": "0"}
            self._receipt("submit_formal_topic", idempotency_key, request, result)
            self._audit(task_id, "formal_topic_submitted", result)
        return result

    def confirm_formal_topic(
        self,
        *,
        task_id: str,
        topic_version_id: str,
        actor: str,
        actor_kind: str,
        reason: str,
        idempotency_key: str,
    ) -> dict[str, str]:
        if actor_kind != "user":
            raise StateTransitionError("only a user may confirm a formal topic")
        task, version = self._task(task_id), self._version(topic_version_id)
        topic_payload = self.get_artifact_payload(topic_version_id)["payload"]
        self._require_configured_domain(
            str(topic_payload.get("domain_label") or topic_payload.get("domain") or ""),
            context="formal topic confirmation",
        )
        self._assert_current_node(task, "formal_topic", "awaiting_human_review", topic_version_id)
        if version["node"] != "formal_topic":
            raise StateTransitionError("version is not a formal topic")
        request = {"task_id": task_id, "topic_version_id": topic_version_id, "actor": actor, "reason": reason}
        replay = self._replay("confirm_formal_topic", idempotency_key, request)
        if replay:
            return replay
        revision = int(task["task_revision"]) + 1
        with self.conn:
            self._decision(task_id, "formal_topic", topic_version_id, "approved", actor, actor_kind, reason)
            self._set_task(task_id, node="research_plan", version_id=topic_version_id, status="not_started", revision=revision)
            result = {"task_id": task_id, "current_node": "research_plan", "task_revision": str(revision)}
            self._receipt("confirm_formal_topic", idempotency_key, request, result)
            self._audit(task_id, "formal_topic_confirmed", result)
        return result

    def create_input_assembly(self, assembly: InputAssembly, *, idempotency_key: str) -> dict[str, str]:
        if assembly.node not in MODEL_BINDINGS:
            raise StateTransitionError(f"{assembly.node} has no model-input assembly in Stage 0")
        task = self._task(assembly.task_id)
        if task["current_node"] != assembly.node or task["current_status"] not in {"not_started", "awaiting_human_review"}:
            raise StateTransitionError("input assembly may only be prepared for the current new or returned node")
        upstream = self._approved_upstream(task, assembly.node, assembly.upstream_version_id)
        request = assembly.payload()
        replay = self._replay("create_input_assembly", idempotency_key, request)
        if replay:
            return replay
        assembly_id, integrity_hash = _id("assembly"), _hash(request)
        with self.conn:
            self.conn.execute(
                "INSERT INTO stage0_input_assembly VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
                (assembly_id, assembly.task_id, assembly.node, upstream, _canonical(request), integrity_hash, self.data_identity, _now()),
            )
            result = {"assembly_id": assembly_id, "input_integrity_hash": integrity_hash}
            self._receipt("create_input_assembly", idempotency_key, request, result)
            self._audit(assembly.task_id, "input_assembly_created", result)
        return result

    def create_node_request(
        self,
        *,
        task_id: str,
        node: str,
        input_assembly_id: str,
        actor: str,
        idempotency_key: str,
    ) -> dict[str, str]:
        if node not in MODEL_BINDINGS:
            raise StateTransitionError(f"{node} is not a model-backed Stage 0 node")
        task = self._task(task_id)
        self._assert_current_node(task, node, "not_started")
        assembly = self._assembly(input_assembly_id)
        if assembly["task_id"] != task_id or assembly["node"] != node or assembly["data_identity"] != self.data_identity:
            raise StateTransitionError("input assembly does not belong to this task/node/identity")
        upstream = self._approved_upstream(task, node, assembly["upstream_version_id"])
        request = {"task_id": task_id, "node": node, "input_assembly_id": input_assembly_id, "actor": actor}
        replay = self._replay("create_node_request", idempotency_key, request)
        if replay:
            return replay
        version_id = _id("version")
        revision = int(task["task_revision"]) + 1
        with self.conn:
            self.conn.execute(
                "INSERT INTO stage0_content_node_version VALUES (?, ?, ?, NULL, ?, ?, ?, NULL, ?, ?, ?, ?, ?)",
                (version_id, task_id, node, upstream, input_assembly_id, "processing", "not_run", self.data_identity, actor, _now(), revision),
            )
            self._set_task(task_id, node=node, version_id=version_id, status="processing", revision=revision)
            result = {"node_version_id": version_id, "task_revision": str(revision)}
            self._receipt("create_node_request", idempotency_key, request, result)
            self._audit(task_id, "node_request_created", {**result, "node": node})
        return result

    def prepare_atomic_skill_binding(self, *, task_id: str, node_version_id: str) -> dict[str, Any]:
        """Return Core-owned receipt metadata; the atomic Skill alone creates the model request."""
        version = self._version(node_version_id)
        task = self._task(task_id)
        if version["task_id"] != task_id or version["node"] not in MODEL_BINDINGS:
            raise StateTransitionError("node version is not a model-backed version of this task")
        self._assert_current_node(task, version["node"], "processing", node_version_id)
        enforce_atomic_skill_runtime_guard(
            entrypoint="stage0_content_core.prepare_atomic_skill_binding",
            operation=f"stage0.{version['node']}",
        )
        assembly = self._assembly(version["input_assembly_id"])
        payload = json.loads(assembly["payload_json"])
        return {
            "stage0_core": {
                "task_id": task_id,
                "node": version["node"],
                "node_version_id": node_version_id,
                "input_assembly_id": version["input_assembly_id"],
                "task_revision": int(task["task_revision"]),
                "prompt_version": payload["prompt_version"],
                "skill_version": payload["skill_version"],
                "data_identity": self.data_identity,
            }
        }

    def complete_node_from_model(
        self,
        *,
        task_id: str,
        node_version_id: str,
        model_run_id: str,
        output_ref: str,
        validation_status: str,
        actor: str,
        expected_task_revision: int,
        idempotency_key: str,
        artifact_payload: dict[str, Any] | None = None,
    ) -> dict[str, str]:
        if validation_status != "passed":
            raise StateTransitionError("only schema-validated model output may await human review")
        task, request_version = self._task(task_id), self._version(node_version_id)
        if int(task["task_revision"]) != expected_task_revision:
            raise StaleResultError("model result is stale because the task revision changed")
        run = self._model_run(model_run_id)
        self._assert_current_node(task, request_version["node"], "processing", node_version_id)
        if run["task_id"] != task_id or run["node_version_id"] != node_version_id or run["status"] != "succeeded":
            raise ModelGatewayRequiredError("model result is not the successful current Gateway run")
        if run["via_model_gateway"] != 1 or run["data_identity"] != self.data_identity:
            raise ModelGatewayRequiredError("formal output requires a matching ModelGateway record")
        request = {"task_id": task_id, "node_version_id": node_version_id, "model_run_id": model_run_id, "output_ref": output_ref}
        replay = self._replay("complete_node_from_model", idempotency_key, request)
        if replay:
            return replay
        output_version_id, revision = _id("version"), int(task["task_revision"]) + 1
        with self.conn:
            self.conn.execute(
                "INSERT INTO stage0_content_node_version VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (output_version_id, task_id, request_version["node"], node_version_id, request_version["upstream_version_id"], request_version["input_assembly_id"], "awaiting_human_review", output_ref, "passed", self.data_identity, actor, _now(), revision),
            )
            if artifact_payload is not None:
                if request_version["node"] not in ARTIFACT_NODES:
                    raise StateTransitionError("artifact payload persistence is only enabled for formal artifact nodes")
                self._insert_artifact_payload(output_version_id, request_version["node"], artifact_payload)
            self.conn.execute("UPDATE stage0_model_run SET output_version_id=? WHERE model_run_id=?", (output_version_id, model_run_id))
            self._set_task(task_id, node=request_version["node"], version_id=output_version_id, status="awaiting_human_review", revision=revision)
            result = {"node_version_id": output_version_id, "task_revision": str(revision)}
            self._receipt("complete_node_from_model", idempotency_key, request, result)
            self._audit(task_id, "model_output_awaiting_human_review", {**result, "model_run_id": model_run_id})
        return result

    def approve_current_node(self, *, task_id: str, version_id: str, actor: str, actor_kind: str, reason: str, idempotency_key: str) -> dict[str, str]:
        task, version = self._task(task_id), self._version(version_id)
        self._assert_current_node(task, version["node"], "awaiting_human_review", version_id)
        topic_payload = self.get_artifact_payload(str(task["topic_version_id"]))["payload"]
        domain_label = str(topic_payload.get("domain_label") or topic_payload.get("domain") or "").strip()
        workflow_mode = get_content_workflow_mode(domain_label) if domain_label else "manual_guard"
        user_only_nodes = {"research_plan", "content_plan", "review"}
        if workflow_mode == "manual_guard":
            user_only_nodes.update({"deep_research", "formal_draft"})
        if version["node"] in user_only_nodes and actor_kind != "user":
            raise StateTransitionError("this confirmation point requires an explicit user decision")
        request = {"task_id": task_id, "version_id": version_id, "actor": actor, "actor_kind": actor_kind, "reason": reason}
        replay = self._replay("approve_current_node", idempotency_key, request)
        if replay:
            return replay
        decision_node = CONFIRMATION_NODE.get(version["node"], version["node"])
        revision = int(task["task_revision"]) + 1
        with self.conn:
            self._decision(task_id, decision_node, version_id, "approved", actor, actor_kind, reason)
            next_node = NEXT_ARTIFACT_NODE.get(version["node"])
            if next_node is None:
                self._set_task(task_id, node="user_final_confirmation", version_id=version_id, status="approved", revision=revision)
            else:
                self._set_task(task_id, node=next_node, version_id=version_id, status="not_started", revision=revision)
            result = {"task_id": task_id, "current_node": next_node or "user_final_confirmation", "task_revision": str(revision)}
            self._receipt("approve_current_node", idempotency_key, request, result)
            self._audit(task_id, "human_approved", {**result, "decision_node": decision_node})
        return result

    def return_current_node(self, *, task_id: str, version_id: str, input_assembly_id: str, actor: str, reason: str, idempotency_key: str) -> dict[str, str]:
        task, previous = self._task(task_id), self._version(version_id)
        self._assert_current_node(task, previous["node"], "awaiting_human_review", version_id)
        assembly = self._assembly(input_assembly_id)
        if assembly["task_id"] != task_id or assembly["node"] != previous["node"] or assembly["upstream_version_id"] != previous["upstream_version_id"]:
            raise StateTransitionError("returned node needs a new matching input assembly")
        request = {"task_id": task_id, "version_id": version_id, "input_assembly_id": input_assembly_id, "actor": actor, "reason": reason}
        replay = self._replay("return_current_node", idempotency_key, request)
        if replay:
            return replay
        replacement, revision = _id("version"), int(task["task_revision"]) + 1
        with self.conn:
            self._decision(task_id, previous["node"], version_id, "returned", actor, "user", reason)
            self.conn.execute(
                "INSERT INTO stage0_content_node_version VALUES (?, ?, ?, ?, ?, ?, ?, NULL, ?, ?, ?, ?, ?)",
                (replacement, task_id, previous["node"], version_id, previous["upstream_version_id"], input_assembly_id, "processing", "not_run", self.data_identity, actor, _now(), revision),
            )
            self._set_task(task_id, node=previous["node"], version_id=replacement, status="processing", revision=revision)
            result = {"node_version_id": replacement, "task_revision": str(revision)}
            self._receipt("return_current_node", idempotency_key, request, result)
            self._audit(task_id, "human_returned_new_version", {**result, "returned_version_id": version_id})
        return result

    def cancel_current_task(self, *, task_id: str, actor: str, reason: str, idempotency_key: str) -> dict[str, str]:
        task = self._task(task_id)
        if task["current_status"] in {"approved", "cancelled"}:
            raise StateTransitionError("task is already terminal")
        request = {"task_id": task_id, "actor": actor, "reason": reason}
        replay = self._replay("cancel_current_task", idempotency_key, request)
        if replay:
            return replay
        revision = int(task["task_revision"]) + 1
        with self.conn:
            self.conn.execute("UPDATE stage0_content_task SET current_status='cancelled', task_revision=?, cancelled_reason=? WHERE task_id=?", (revision, reason, task_id))
            result = {"task_id": task_id, "task_revision": str(revision)}
            self._receipt("cancel_current_task", idempotency_key, request, result)
            self._audit(task_id, "task_cancelled", result)
        return result

    def _persist_experience_candidate_gateway_envelope(self, envelope: ModelRunEnvelope, binding: dict[str, Any]) -> str:
        if binding.get("data_identity") != self.data_identity:
            raise DataIdentityError("experience candidate model run identity does not match Core identity")
        candidate_id = str(binding.get("experience_candidate_id") or "")
        candidate = self.conn.execute(
            "SELECT status FROM stage0_experience_candidate WHERE experience_candidate_id=? AND data_identity=?",
            (candidate_id, self.data_identity),
        ).fetchone()
        if candidate is None or str(candidate["status"]) != "preparing":
            raise ModelGatewayRequiredError("experience candidate model run does not belong to an open candidate")
        model_run_id = _id("experience_candidate_model_run")
        with self.conn:
            self.conn.execute(
                "INSERT INTO stage0_experience_candidate_model_run VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (
                    model_run_id, candidate_id, envelope.status, envelope.route_name, envelope.provider_name,
                    envelope.model_name, envelope.input_hash, envelope.output_hash,
                    _canonical(envelope.as_payload()), self.data_identity, _now(),
                ),
            )
            self._audit(None, "experience_candidate_model_run_recorded", {
                "experience_candidate_id": candidate_id,
                "experience_candidate_model_run_id": model_run_id,
                "status": envelope.status,
            })
        return model_run_id

    def _persist_gateway_envelope(self, envelope: ModelRunEnvelope, binding: dict[str, Any]) -> str:
        if binding.get("data_identity") != self.data_identity:
            raise DataIdentityError("ModelGateway data identity does not match Core identity")
        task_id, node_version_id = str(binding.get("task_id") or ""), str(binding.get("node_version_id") or "")
        task, version = self._task(task_id), self._version(node_version_id)
        if int(binding.get("task_revision", -1)) != int(task["task_revision"]):
            raise StaleResultError("ModelGateway envelope belongs to a stale task revision")
        self._assert_current_node(task, version["node"], "processing", node_version_id)
        if version["node"] != binding.get("node") or version["input_assembly_id"] != binding.get("input_assembly_id"):
            raise ModelGatewayRequiredError("ModelGateway envelope does not match the active node input")
        assembly = self._assembly(version["input_assembly_id"])
        model_run_id = _id("model_run")
        with self.conn:
            self.conn.execute(
                """
                INSERT INTO stage0_model_run(
                    model_run_id, task_id, node, node_version_id, input_assembly_id, status, request_id,
                    prompt_version, skill_version, route_id, route_version, provider_ref, provider_name,
                    model_name, input_integrity_hash, output_hash, output_version_id, validation_status,
                    error_json, retry_status, usage_json, cost_json, duration_ms, data_identity, created_at,
                    via_model_gateway
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    model_run_id, task_id, version["node"], node_version_id, version["input_assembly_id"],
                    envelope.status, envelope.correlation_id, binding["prompt_version"], binding["skill_version"],
                    envelope.route_id or envelope.route_name, envelope.config_version, envelope.provider_ref,
                    envelope.provider_name, envelope.model_name, assembly["integrity_hash"], envelope.output_hash,
                    None, "not_validated", _canonical(envelope.error or {}), "not_retried",
                    _canonical({"prompt_tokens": envelope.usage.prompt_tokens, "completion_tokens": envelope.usage.completion_tokens, "total_tokens": envelope.usage.total_tokens}),
                    _canonical(envelope.cost), envelope.duration_ms, self.data_identity, _now(), 1,
                ),
            )
            self._audit(task_id, "model_gateway_envelope_recorded", {"model_run_id": model_run_id, "node": version["node"], "status": envelope.status})
        return model_run_id

    def _task(self, task_id: str) -> sqlite3.Row:
        row = self.conn.execute("SELECT * FROM stage0_content_task WHERE task_id=?", (task_id,)).fetchone()
        if row is None or row["data_identity"] != self.data_identity:
            raise StateTransitionError("task does not exist in this data identity")
        return row

    def _version(self, version_id: str) -> sqlite3.Row:
        row = self.conn.execute("SELECT * FROM stage0_content_node_version WHERE version_id=?", (version_id,)).fetchone()
        if row is None or row["data_identity"] != self.data_identity:
            raise StateTransitionError("node version does not exist in this data identity")
        return row

    def _assembly(self, assembly_id: str) -> sqlite3.Row:
        row = self.conn.execute("SELECT * FROM stage0_input_assembly WHERE assembly_id=?", (assembly_id,)).fetchone()
        if row is None or row["data_identity"] != self.data_identity:
            raise StateTransitionError("input assembly does not exist in this data identity")
        return row

    def _model_run(self, model_run_id: str) -> sqlite3.Row:
        row = self.conn.execute("SELECT * FROM stage0_model_run WHERE model_run_id=?", (model_run_id,)).fetchone()
        if row is None:
            raise ModelGatewayRequiredError("model run does not exist")
        return row

    def _discovery_run(self, run_id: str) -> sqlite3.Row:
        row = self.conn.execute("SELECT * FROM stage1b_discovery_run WHERE run_id=?", (run_id,)).fetchone()
        if row is None or row["data_identity"] != self.data_identity:
            raise StateTransitionError("discovery run does not exist in this data identity")
        return row

    def _validate_discovery_execution_mode(self, execution_mode: str) -> None:
        if execution_mode not in DISCOVERY_EXECUTION_MODES:
            raise StateTransitionError("discovery execution mode is invalid")
        if self.data_identity in NON_PRODUCTION_IDENTITIES and execution_mode != "test_isolated":
            raise DataIdentityError("non-production discovery data must use test_isolated mode")
        if self.data_identity == "production" and execution_mode == "test_isolated":
            raise DataIdentityError("production discovery data cannot use test_isolated mode")

    def _discovery_context_optional(self, run_id: str) -> sqlite3.Row | None:
        row = self.conn.execute(
            "SELECT * FROM stage1b_run_execution_context WHERE run_id=?", (run_id,)
        ).fetchone()
        if row is not None and row["data_identity"] != self.data_identity:
            raise StateTransitionError("discovery execution context does not exist in this data identity")
        return row

    def _discovery_context(self, run_id: str) -> sqlite3.Row:
        row = self._discovery_context_optional(run_id)
        if row is None:
            raise StateTransitionError(
                "discovery run has no execution classification; it cannot be viewed or used"
            )
        return row

    def _discovery_source(self, source_version_id: str) -> sqlite3.Row:
        row = self.conn.execute("SELECT * FROM stage1b_source_version WHERE source_version_id=?", (source_version_id,)).fetchone()
        if row is None or row["data_identity"] != self.data_identity:
            raise StateTransitionError("discovery source does not exist in this data identity")
        return row

    def _discovery_assembly(self, assembly_id: str) -> sqlite3.Row:
        row = self.conn.execute("SELECT * FROM stage1b_input_assembly WHERE assembly_id=?", (assembly_id,)).fetchone()
        if row is None or row["data_identity"] != self.data_identity:
            raise StateTransitionError("discovery input assembly does not exist in this data identity")
        return row

    def _discovery_model_run(self, model_run_id: str) -> sqlite3.Row:
        row = self.conn.execute("SELECT * FROM stage1b_model_run WHERE model_run_id=?", (model_run_id,)).fetchone()
        if row is None or row["data_identity"] != self.data_identity:
            raise ModelGatewayRequiredError("discovery model run does not exist in this data identity")
        return row

    def _discovery_candidate(self, candidate_version_id: str) -> sqlite3.Row:
        row = self.conn.execute("SELECT * FROM stage1b_candidate_version WHERE candidate_version_id=?", (candidate_version_id,)).fetchone()
        if row is None or row["data_identity"] != self.data_identity:
            raise StateTransitionError("discovery candidate does not exist in this data identity")
        return row

    def _candidate_pool_state(self, candidate_version_id: str) -> sqlite3.Row:
        row = self.conn.execute(
            "SELECT * FROM stage1b_candidate_pool_state WHERE candidate_version_id=? AND data_identity=? "
            "ORDER BY effective_at DESC, pool_state_id DESC LIMIT 1",
            (candidate_version_id, self.data_identity),
        ).fetchone()
        if row is None:
            raise StateTransitionError("discovery candidate has no formal pool state")
        return row

    def _candidate_pool_parameter(self, as_of: str | None = None) -> sqlite3.Row:
        if as_of is None:
            row = self.conn.execute(
                "SELECT * FROM stage1b_candidate_pool_parameter_version WHERE data_identity=? "
                "ORDER BY effective_at DESC, parameter_version DESC LIMIT 1",
                (self.data_identity,),
            ).fetchone()
        else:
            row = self.conn.execute(
                "SELECT * FROM stage1b_candidate_pool_parameter_version WHERE data_identity=? AND effective_at<=? "
                "ORDER BY effective_at DESC, parameter_version DESC LIMIT 1",
                (self.data_identity, as_of),
            ).fetchone()
        if row is None:
            raise StateTransitionError("candidate pool observation window is not configured")
        return row

    def find_related_discovery_candidates(
        self, *, domain_label: str, core_question: str, topic_angle: str
    ) -> dict[str, list[str]]:
        """Use the already-frozen candidate judgement to reuse the same question/angle instead of silently duplicating it."""
        same_angle: list[str] = []
        different_angle: list[str] = []
        rows = self.conn.execute(
            "SELECT candidate_version_id, payload_json FROM stage1b_candidate_version "
            "WHERE domain_label=? AND data_identity=? AND status='awaiting_user_decision' "
            "AND NOT EXISTS (SELECT 1 FROM stage1b_candidate_decision decision WHERE decision.candidate_version_id=stage1b_candidate_version.candidate_version_id)",
            (domain_label, self.data_identity),
        ).fetchall()
        normalized_question = core_question.strip().casefold()
        normalized_angle = topic_angle.strip().casefold()
        for row in rows:
            payload = json.loads(row["payload_json"])
            if str(payload.get("core_question", "")).strip().casefold() != normalized_question:
                continue
            if str(payload.get("topic_angle", "")).strip().casefold() == normalized_angle:
                same_angle.append(str(row["candidate_version_id"]))
            else:
                different_angle.append(str(row["candidate_version_id"]))
        return {"same_angle": same_angle, "different_angle": different_angle}

    def _resolve_discovery_model_route(self):
        router = ModelRouter.from_file()
        definition = router.routes.get("business_analysis")
        if definition is None or definition.fallback != "none" or "topic_screening" not in definition.allowed_task_types:
            raise ModelBindingUnavailableError("daily discovery requires the explicit business topic_screening route")
        try:
            return router.resolve_bound_route(
                "business_analysis",
                route_name="business.source_to_topic",
                parameters={"stream": False},
            )
        except Exception as exc:
            raise ModelBindingUnavailableError("daily discovery has no explicit resolved model binding") from exc

    def _assert_discovery_source_provenance(
        self,
        *,
        domain_label: str,
        source_type: str,
        source_object_id: str,
        source_object_version: str,
        source_time: str,
        payload: dict[str, Any],
    ) -> None:
        """Accept only a source whose precise formal origin still matches the Core facts."""
        origin = payload.get("formal_source")
        if not isinstance(origin, dict):
            raise StateTransitionError("daily discovery source requires a formal_source mapping")
        if source_type == "daily_competitor_content":
            row = self.conn.execute(
                "SELECT video.video_id, video.last_checked_at, video.publish_time, video.title, video.url, video.raw_json, "
                "video.raw_archive_ref, video.excluded_reason, account.domain_label, account.registration_status, "
                "account.source_config_ref FROM competitor_videos video JOIN competitor_accounts account "
                "ON account.account_id=video.account_id WHERE video.video_id=?",
                (source_object_id,),
            ).fetchone()
            expected_table, expected_version = "competitor_videos", "last_checked_at"
        elif source_type == "historical_high_signal":
            row = self.conn.execute(
                "SELECT hit.hit_id, hit.promoted_at, hit.publish_time, hit.title, hit.url, hit.judgment_confidence, "
                "video.raw_json, video.raw_archive_ref, video.excluded_reason, account.domain_label, "
                "account.registration_status, account.source_config_ref FROM hits hit "
                "JOIN competitor_videos video ON video.video_id=hit.video_id "
                "JOIN competitor_accounts account ON account.account_id=hit.account_id WHERE hit.hit_id=?",
                (source_object_id,),
            ).fetchone()
            expected_table, expected_version = "hits", "promoted_at"
        elif source_type == "hotspot":
            row = self.conn.execute(
                "SELECT observation.*, run.status FROM trendradar_hotspot_observation observation "
                "JOIN trendradar_collection_run run ON run.collection_run_id=observation.collection_run_id "
                "WHERE observation.observation_id=? AND run.status IN ('completed', 'completed_with_failures')",
                (source_object_id,),
            ).fetchone()
            expected_table, expected_version = "trendradar_hotspot_observation", "observed_at"
        elif source_type == "tag_discovery":
            row = self.conn.execute(
                "SELECT video.*, tag.tag FROM discovered_external_videos video "
                "JOIN domain_search_tags tag ON tag.tag_id=video.tag_id "
                "WHERE video.discovered_video_id=? AND video.domain_label=? AND tag.status='active'",
                (source_object_id, domain_label),
            ).fetchone()
            expected_table, expected_version = "discovered_external_videos", "discovered_at"
        elif source_type == "question_expansion":
            row = self.conn.execute(
                "SELECT * FROM stage1_question_expansion_source WHERE expansion_id=? "
                "AND domain_label=? AND validation_outcome='supported' AND data_identity=?",
                (source_object_id, domain_label, self.data_identity),
            ).fetchone()
            expected_table, expected_version = "stage1_question_expansion_source", "integrity_hash"
        elif source_type == "saved_user_direction":
            row = self.conn.execute(
                "SELECT * FROM stage1_saved_user_direction_source WHERE direction_id=? "
                "AND domain_label=? AND status='active' AND data_identity=?",
                (source_object_id, domain_label, self.data_identity),
            ).fetchone()
            expected_table, expected_version = "stage1_saved_user_direction_source", "integrity_hash"
        else:
            raise StateTransitionError("daily discovery source type is not enabled in this Stage 1B slice")
        if row is None:
            raise StateTransitionError("daily discovery source is not registered in the formal source facts")
        if source_type == "hotspot":
            match_terms = tuple(str(term) for term in get_discovery_policy(domain_label).get("hotspot_match_terms", []))
            folded_title = str(row["title"]).casefold()
            if not payload.get("hotspot_event_cluster") and (
                not match_terms or not any(term.casefold() in folded_title for term in match_terms)
            ):
                raise StateTransitionError("hotspot does not match the versioned domain policy")
        if origin.get("table") != expected_table or origin.get("object_id") != source_object_id:
            raise StateTransitionError("daily discovery source mapping does not identify the formal origin")
        formal_version = str(row[expected_version])
        if origin.get("object_version") != formal_version or source_object_version != formal_version:
            raise StaleResultError("daily discovery source version does not match the formal origin")
        if source_type in {"daily_competitor_content", "historical_high_signal"}:
            expected_time_field = "publish_time"
        elif source_type == "question_expansion":
            expected_time_field = "validated_at"
        elif source_type == "saved_user_direction":
            expected_time_field = "saved_at"
        else:
            expected_time_field = expected_version
        if source_time != str(row[expected_time_field]):
            raise StateTransitionError("daily discovery source time does not match the formal origin")
        if source_type in {"daily_competitor_content", "historical_high_signal"}:
            if domain_label != str(row["domain_label"]):
                raise StateTransitionError("daily discovery source domain does not match the formal origin")
            if row["registration_status"] != "active" or not str(row["source_config_ref"] or "").strip():
                raise StateTransitionError("daily discovery source account is not qualified")
            if not str(row["raw_archive_ref"] or "").strip() or row["excluded_reason"] is not None:
                raise StateTransitionError("daily discovery source lacks qualified formal material")
            if source_type == "historical_high_signal" and row["judgment_confidence"] not in {"rough", "formal"}:
                raise StateTransitionError("historical source lacks a recorded high-signal qualification")
        if source_type in {"question_expansion", "saved_user_direction"}:
            if not str(row["core_question"] or "").strip():
                raise StateTransitionError("daily discovery source lacks a concrete core question")
            raw_payload = row["payload_json"]
        else:
            if not str(row["title"] or "").strip() or not str(row["url"] or "").strip():
                raise StateTransitionError("daily discovery source lacks required formal material")
            raw_payload = row["raw_json"]
        try:
            raw_hash = _hash(json.loads(str(raw_payload)))
        except (TypeError, ValueError, json.JSONDecodeError) as exc:
            raise StateTransitionError("daily discovery source has unreadable formal raw payload") from exc
        if origin.get("raw_metadata_hash") != raw_hash:
            raise StaleResultError("daily discovery source raw payload does not match the formal origin")

    def get_task(self, task_id: str) -> dict[str, Any]:
        row = self._task(task_id)
        return {key: row[key] for key in row.keys()}

    def get_node_version(self, version_id: str) -> dict[str, Any]:
        row = self._version(version_id)
        return {key: row[key] for key in row.keys()}

    def get_input_assembly_payload(self, assembly_id: str) -> dict[str, Any]:
        row = self._assembly(assembly_id)
        return json.loads(row["payload_json"])

    def find_command_replay(self, command: str, idempotency_key: str, request: dict[str, Any]) -> dict[str, str] | None:
        return self._replay(command, idempotency_key, request)

    def record_completed_command(
        self,
        *,
        command: str,
        idempotency_key: str,
        request: dict[str, Any],
        task_id: str,
        event: str,
        result: dict[str, str],
    ) -> None:
        """Persist an orchestration receipt and audit event through the Core boundary."""
        with self.conn:
            self._receipt(command, idempotency_key, request, result)
            self._audit(task_id, event, result)

    def record_model_validation_failure(
        self,
        *,
        task_id: str,
        node_version_id: str,
        model_run_id: str,
        reason: str,
        raw_model_output: str | None = None,
    ) -> None:
        """Record a rejected structured output without creating a business artifact."""
        self.fail_current_node_from_model(
            task_id=task_id,
            node_version_id=node_version_id,
            model_run_id=model_run_id,
            failure_stage="model_output_validation",
            reason=reason,
            raw_model_output=raw_model_output,
        )

    def fail_current_node_from_model(
        self,
        *,
        task_id: str,
        node_version_id: str,
        model_run_id: str | None,
        failure_stage: str,
        reason: str,
        raw_model_output: str | None = None,
    ) -> None:
        """Make a model failure terminal and visible without fabricating an artifact."""
        task, version = self._task(task_id), self._version(node_version_id)
        self._assert_current_node(task, version["node"], "processing", node_version_id)
        if not failure_stage.strip() or not reason.strip():
            raise StateTransitionError("a model failure needs a stage and reason")
        model_run = self._model_run(model_run_id) if model_run_id is not None else None
        if model_run is not None and (
            model_run["data_identity"] != self.data_identity
            or model_run["task_id"] != task_id
            or model_run["node_version_id"] != node_version_id
        ):
            raise ModelGatewayRequiredError("model run does not belong to the active node request")
        failure = {
            "failure_stage": failure_stage,
            "reason": reason,
            "raw_model_output": raw_model_output,
            "raw_model_output_status": "available" if raw_model_output is not None else "not_available",
            "automatic_retry": False,
        }
        revision = int(task["task_revision"]) + 1
        failed_version_id = _id("version")
        with self.conn:
            if model_run_id is not None:
                self.conn.execute(
                    "UPDATE stage0_model_run SET validation_status='failed', error_json=? WHERE model_run_id=?",
                    (_canonical(failure), model_run_id),
                )
            self.conn.execute(
                "INSERT INTO stage0_content_node_version VALUES (?, ?, ?, ?, ?, ?, ?, NULL, ?, ?, ?, ?, ?)",
                (
                    failed_version_id, task_id, version["node"], node_version_id,
                    version["upstream_version_id"], version["input_assembly_id"], "failed", "failed",
                    self.data_identity, "model_failure_record", _now(), revision,
                ),
            )
            self.conn.execute(
                "INSERT INTO stage0_content_node_failure VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (
                    _id("node_failure"), task_id, failed_version_id, node_version_id,
                    model_run_id, failure_stage, reason, raw_model_output,
                    failure["raw_model_output_status"], self.data_identity, _now(),
                ),
            )
            self._set_task(
                task_id, node=str(version["node"]), version_id=failed_version_id,
                status="failed", revision=revision,
            )
            self._audit(task_id, "model_node_failed", {
                "node_version_id": failed_version_id,
                "request_version_id": node_version_id,
                "model_run_id": model_run_id,
                **failure,
            })

    def create_discovery_run(
        self,
        *,
        discovery_date: str,
        actor: str,
        execution_mode: str,
        domains: tuple[str, ...],
        idempotency_key: str,
    ) -> dict[str, str]:
        self._validate_discovery_execution_mode(execution_mode)
        domain_scope = tuple(str(domain).strip() for domain in domains)
        if not domain_scope or any(not domain for domain in domain_scope):
            raise StateTransitionError("discovery run requires at least one formal domain")
        if len(set(domain_scope)) != len(domain_scope):
            raise StateTransitionError("discovery run domain scope may not contain duplicates")
        unknown_domains = sorted(set(domain_scope) - set(formal_domain_labels()))
        if unknown_domains:
            raise StateTransitionError(
                f"discovery run contains unconfigured formal domains: {unknown_domains}"
            )
        request = {
            "discovery_date": discovery_date,
            "actor": actor,
            "execution_mode": execution_mode,
            "domains": list(domain_scope),
            "data_identity": self.data_identity,
        }
        replay = self._replay("stage1b_create_discovery_run", idempotency_key, request)
        if replay:
            return replay
        run_id = _id("discovery_run")
        with self.conn:
            self.conn.execute(
                "INSERT INTO stage1b_discovery_run VALUES (?, ?, 'processing', ?, ?, ?, NULL, NULL)",
                (run_id, discovery_date, self.data_identity, actor, _now()),
            )
            self.conn.execute(
                "INSERT INTO stage1b_run_execution_context VALUES (?, ?, 'processing', ?, ?, ?, ?)",
                (run_id, execution_mode, "run created with explicit execution mode", actor, _now(), self.data_identity),
            )
            recorded_at = _now()
            self.conn.executemany(
                "INSERT INTO stage1b_run_domain_scope "
                "(run_id, domain_label, data_identity, recorded_at) VALUES (?, ?, ?, ?)",
                (
                    (run_id, domain_label, self.data_identity, recorded_at)
                    for domain_label in domain_scope
                ),
            )
            result = {"run_id": run_id, "discovery_date": discovery_date}
            self._receipt("stage1b_create_discovery_run", idempotency_key, request, result)
            self._audit(
                run_id,
                "stage1b_discovery_run_started",
                {**result, "execution_mode": execution_mode, "domains": list(domain_scope)},
            )
        return result

    def register_question_expansion_source(
        self,
        *,
        expansion_id: str,
        domain_label: str,
        core_question: str,
        parent_source_ref: dict[str, Any],
        actor: str,
    ) -> dict[str, str]:
        """Register only an already-supported bounded expansion as a source."""
        if domain_label not in formal_domain_labels():
            raise StateTransitionError("question expansion requires a configured formal domain")
        if len(core_question.strip()) < 6 or not parent_source_ref:
            raise StateTransitionError("question expansion requires a concrete question and parent source")
        payload = {
            "title": core_question.strip(),
            "core_question": core_question.strip(),
            "parent_source_ref": parent_source_ref,
            "validation_outcome": "supported",
        }
        integrity_hash = _hash(payload)
        validated_at = _now()
        with self.conn:
            self.conn.execute(
                """
                INSERT INTO stage1_question_expansion_source(
                    expansion_id, domain_label, core_question, parent_source_ref_json,
                    validation_outcome, validated_at, payload_json, integrity_hash,
                    data_identity, created_by
                ) VALUES (?, ?, ?, ?, 'supported', ?, ?, ?, ?, ?)
                """,
                (expansion_id, domain_label, core_question.strip(), _canonical(parent_source_ref),
                 validated_at, _canonical(payload), integrity_hash, self.data_identity, actor),
            )
        return {"expansion_id": expansion_id, "source_object_version": integrity_hash, "validated_at": validated_at}

    def register_saved_user_direction_source(
        self,
        *,
        direction_id: str,
        domain_label: str,
        core_question: str,
        submitted_by: str,
        account_ref: str | None = None,
    ) -> dict[str, str]:
        """Register a user-supplied concrete question without inventing an angle."""
        if domain_label not in formal_domain_labels():
            raise StateTransitionError("saved user direction requires a configured formal domain")
        if len(core_question.strip()) < 6 or not submitted_by.strip():
            raise StateTransitionError("saved user direction requires a concrete question and user identity")
        normalized_account_ref = (
            account_ref.strip() if account_ref and account_ref.strip() else None
        )
        payload = {
            "title": core_question.strip(),
            "core_question": core_question.strip(),
            "submitted_by": submitted_by.strip(),
            "account_ref": normalized_account_ref,
        }
        integrity_hash = _hash(payload)
        saved_at = _now()
        with self.conn:
            manual_source_id = f"manual_direction_{direction_id}"
            manual_original_content = {"core_question": core_question.strip()}
            manual_integrity_hash = _hash(
                {
                    "source_kind": "direction",
                    "original_content": manual_original_content,
                    "account_ref": normalized_account_ref,
                    "evidence_role": "user_origin_not_fact",
                }
            )
            self.conn.execute(
                """
                INSERT INTO stage0_manual_source(
                    manual_source_id, domain_label, account_ref, source_kind, original_content_json,
                    submitted_by, submitted_at, evidence_role, integrity_hash, data_identity
                ) VALUES (?, ?, ?, 'direction', ?, ?, ?, 'user_origin_not_fact', ?, ?)
                """,
                (
                    manual_source_id, domain_label, normalized_account_ref,
                    _canonical(manual_original_content), submitted_by.strip(),
                    saved_at, manual_integrity_hash, self.data_identity,
                ),
            )
            self.conn.execute(
                """
                INSERT INTO stage1_saved_user_direction_source(
                    direction_id, domain_label, core_question, submitted_by, saved_at,
                    payload_json, integrity_hash, status, data_identity
                ) VALUES (?, ?, ?, ?, ?, ?, ?, 'active', ?)
                """,
                (direction_id, domain_label, core_question.strip(), submitted_by.strip(), saved_at,
                 _canonical(payload), integrity_hash, self.data_identity),
            )
            self.conn.execute(
                """
                INSERT INTO stage0_manual_source_link(
                    link_id, manual_source_id, target_kind, target_id, data_identity, linked_at
                ) VALUES (?, ?, 'saved_user_direction', ?, ?, ?)
                """,
                (_id("manual_source_link"), manual_source_id, direction_id, self.data_identity, saved_at),
            )
        return {"direction_id": direction_id, "source_object_version": integrity_hash, "saved_at": saved_at}

    def register_manual_source(
        self,
        *,
        manual_source_id: str,
        domain_label: str,
        account_ref: str | None,
        source_kind: str,
        original_content: Any,
        submitted_by: str,
    ) -> dict[str, str]:
        """Persist the user's original input as origin only, never as factual proof."""
        if domain_label not in formal_domain_labels():
            raise StateTransitionError("manual source requires a configured formal domain")
        if source_kind not in MANUAL_SOURCE_KINDS:
            raise StateTransitionError("manual source kind is not supported")
        if not manual_source_id.strip() or not submitted_by.strip():
            raise StateTransitionError("manual source requires an identifier and user identity")
        if original_content is None or (isinstance(original_content, str) and not original_content.strip()):
            raise StateTransitionError("manual source requires the original user content")
        payload = {
            "source_kind": source_kind,
            "original_content": original_content,
            "account_ref": account_ref.strip() if account_ref and account_ref.strip() else None,
            "evidence_role": "user_origin_not_fact",
        }
        integrity_hash = _hash(payload)
        submitted_at = _now()
        with self.conn:
            self.conn.execute(
                """
                INSERT INTO stage0_manual_source(
                    manual_source_id, domain_label, account_ref, source_kind, original_content_json,
                    submitted_by, submitted_at, evidence_role, integrity_hash, data_identity
                ) VALUES (?, ?, ?, ?, ?, ?, ?, 'user_origin_not_fact', ?, ?)
                """,
                (
                    manual_source_id.strip(), domain_label, payload["account_ref"], source_kind,
                    _canonical(original_content), submitted_by.strip(), submitted_at,
                    integrity_hash, self.data_identity,
                ),
            )
        return {"manual_source_id": manual_source_id.strip(), "source_object_version": integrity_hash, "submitted_at": submitted_at}

    def list_manual_source_workbench(self) -> list[dict[str, Any]]:
        rows = self.conn.execute(
            "SELECT * FROM stage0_manual_source WHERE data_identity=? "
            "ORDER BY submitted_at DESC, manual_source_id DESC",
            (self.data_identity,),
        ).fetchall()
        results: list[dict[str, Any]] = []
        for row in rows:
            exploration = self.conn.execute(
                "SELECT exploration.exploration_id, exploration.exploration_kind, "
                "exploration.scope_json, state.status, state.reason, state.effective_at, "
                "(SELECT COUNT(*) FROM stage0_manual_exploration_material material "
                "WHERE material.exploration_id=exploration.exploration_id "
                "AND material.data_identity=?) AS material_count "
                "FROM stage0_manual_exploration exploration "
                "LEFT JOIN stage0_manual_exploration_state state "
                "ON state.exploration_state_id=("
                "SELECT newest.exploration_state_id "
                "FROM stage0_manual_exploration_state newest "
                "WHERE newest.exploration_id=exploration.exploration_id "
                "AND newest.data_identity=? "
                "ORDER BY newest.effective_at DESC, newest.exploration_state_id DESC LIMIT 1"
                ") WHERE exploration.manual_source_id=? AND exploration.data_identity=?",
                (
                    self.data_identity,
                    self.data_identity,
                    row["manual_source_id"],
                    self.data_identity,
                ),
            ).fetchone()
            links = self.conn.execute(
                "SELECT target_kind, target_id, linked_at "
                "FROM stage0_manual_source_link "
                "WHERE manual_source_id=? AND data_identity=? "
                "ORDER BY linked_at, link_id",
                (row["manual_source_id"], self.data_identity),
            ).fetchall()
            exploration_materials = (
                self.conn.execute(
                    "SELECT material_ref_json, evidence_role, collected_at "
                    "FROM stage0_manual_exploration_material "
                    "WHERE exploration_id=? AND data_identity=? "
                    "ORDER BY collected_at, exploration_material_id",
                    (exploration["exploration_id"], self.data_identity),
                ).fetchall()
                if exploration is not None
                else []
            )
            results.append(
                {
                    "manual_source_id": str(row["manual_source_id"]),
                    "domain_label": str(row["domain_label"]),
                    "account_ref": row["account_ref"],
                    "source_kind": str(row["source_kind"]),
                    "original_content": json.loads(str(row["original_content_json"])),
                    "submitted_by": str(row["submitted_by"]),
                    "submitted_at": str(row["submitted_at"]),
                    "evidence_role": str(row["evidence_role"]),
                    "exploration": (
                        {
                            **{
                                key: exploration[key]
                                for key in exploration.keys()
                                if key != "scope_json"
                            },
                            "scope": json.loads(str(exploration["scope_json"])),
                            "material_count": int(exploration["material_count"] or 0),
                            "materials": [
                                {
                                    "material": json.loads(
                                        str(material["material_ref_json"])
                                    ),
                                    "evidence_role": str(
                                        material["evidence_role"]
                                    ),
                                    "collected_at": str(material["collected_at"]),
                                }
                                for material in exploration_materials
                            ],
                        }
                        if exploration is not None
                        else None
                    ),
                    "links": [
                        {key: link[key] for key in link.keys()} for link in links
                    ],
                }
            )
        return results

    def select_experience_candidate_sources(self, *, task_id: str) -> dict[str, Any] | None:
        """Choose one bounded, same-type source set without inferring a reusable conclusion."""
        task = self._task(task_id)
        if task["current_node"] != "content_plan" or task["current_status"] != "not_started":
            return None
        topic = self.get_artifact_payload(str(task["topic_version_id"]))["payload"]
        domain_label = str(topic.get("domain_label") or "").strip()
        if not domain_label:
            return None
        existing = self.conn.execute(
            "SELECT experience_candidate_id FROM stage0_experience_candidate WHERE task_id=? AND data_identity=?",
            (task_id, self.data_identity),
        ).fetchone()
        if existing is not None:
            return None
        rows = self.conn.execute(
            "SELECT item.item_ref, item.artifact_json FROM stage0_competitor_registration_item item "
            "JOIN stage0_competitor_registration registration ON registration.registration_id=item.registration_id "
            "JOIN stage0_content_account account ON account.content_account_id=registration.competitor_account_id "
            "WHERE item.step_name='breakdown' AND item.status='completed' AND item.data_identity=? "
            "AND registration.data_identity=? AND account.data_identity=? AND account.domain_label=? "
            "ORDER BY item.updated_at, item.item_ref",
            (self.data_identity, self.data_identity, self.data_identity, domain_label),
        ).fetchall()
        groups: dict[tuple[str, str], list[dict[str, Any]]] = {}
        for row in rows:
            try:
                artifact = json.loads(str(row["artifact_json"]))
                breakdown = artifact.get("deep_breakdown")
                if not isinstance(breakdown, dict):
                    continue
                subject = str(breakdown.get("content_subject_type") or "unclear")
                form = str(breakdown.get("expression_form") or "unclear")
                if subject == "unclear" or form == "unclear":
                    continue
                source_id = str(breakdown.get("source_id") or row["item_ref"])
                source = {
                    "source_id": source_id,
                    "content_subject_type": subject,
                    "expression_form": form,
                    "content_type_evidence": breakdown.get("content_type_evidence") or [],
                    "spoken_progression": breakdown.get("spoken_progression") or [],
                    "writing_methods": breakdown.get("writing_methods") or [],
                    "reference_boundary": breakdown.get("reference_boundary") or {},
                    "cannot_infer": breakdown.get("cannot_infer") or [],
                }
            except (TypeError, ValueError, json.JSONDecodeError):
                continue
            groups.setdefault((subject, form), []).append(source)
        eligible = [(key, value[:6]) for key, value in groups.items() if len(value) >= 2]
        for (subject, form), sources in sorted(
            eligible, key=lambda item: (-len(item[1]), item[0][0], item[0][1])
        ):
            fingerprint = _hash({"domain_label": domain_label, "source_ids": sorted(item["source_id"] for item in sources)})
            seen = self.conn.execute(
                "SELECT 1 FROM stage0_experience_candidate WHERE domain_label=? AND source_fingerprint=? AND data_identity=? LIMIT 1",
                (domain_label, fingerprint, self.data_identity),
            ).fetchone()
            if seen is None:
                return {
                    "task_id": task_id,
                    "domain_label": domain_label,
                    "content_type": {"content_subject_type": subject, "expression_form": form},
                    "sources": sources,
                }
        return None

    def open_experience_candidate(
        self,
        *,
        task_id: str,
        domain_label: str,
        frozen_sources: list[dict[str, Any]],
        actor: str,
    ) -> dict[str, str]:
        if not actor.strip() or len(frozen_sources) < 2:
            raise StateTransitionError("experience candidate requires at least two frozen source breakdowns and an actor")
        source_ids = [str(item.get("source_id") or "").strip() for item in frozen_sources]
        if any(not source_id for source_id in source_ids) or len(set(source_ids)) != len(source_ids):
            raise StateTransitionError("experience candidate sources need unique source IDs")
        fingerprint = _hash({"domain_label": domain_label, "source_ids": sorted(source_ids)})
        existing = self.conn.execute(
            "SELECT experience_candidate_id, status FROM stage0_experience_candidate WHERE task_id=? AND source_fingerprint=? AND data_identity=?",
            (task_id, fingerprint, self.data_identity),
        ).fetchone()
        if existing is not None:
            return {"experience_candidate_id": str(existing["experience_candidate_id"]), "status": str(existing["status"])}
        candidate_id = _id("experience_candidate")
        with self.conn:
            self.conn.execute(
                "INSERT INTO stage0_experience_candidate VALUES (?, ?, ?, ?, ?, 'preparing', NULL, '{}', ?, ?, ?, NULL, NULL, NULL)",
                (candidate_id, task_id, domain_label, fingerprint, _canonical(frozen_sources), self.data_identity, actor.strip(), _now()),
            )
            self._audit(task_id, "experience_candidate_opened", {"experience_candidate_id": candidate_id, "source_count": len(source_ids)})
        return {"experience_candidate_id": candidate_id, "status": "preparing"}

    def complete_experience_candidate(
        self, *, experience_candidate_id: str, proposal: dict[str, Any], model_run_id: str
    ) -> dict[str, str]:
        candidate = self._experience_candidate(experience_candidate_id)
        if candidate["status"] != "preparing" or not model_run_id:
            raise StateTransitionError("experience candidate is not ready to receive a model result")
        status = "awaiting_human_decision" if proposal.get("decision") == "proposal" else "no_proposal"
        with self.conn:
            self.conn.execute(
                "UPDATE stage0_experience_candidate SET status=?, proposal_json=? WHERE experience_candidate_id=?",
                (status, _canonical(proposal), experience_candidate_id),
            )
            self._audit(str(candidate["task_id"]), "experience_candidate_completed", {"experience_candidate_id": experience_candidate_id, "status": status, "model_run_id": model_run_id})
        return {"experience_candidate_id": experience_candidate_id, "status": status}

    def fail_experience_candidate(
        self, *, experience_candidate_id: str, reason: str, model_run_id: str | None,
        raw_model_output: str | None = None,
    ) -> dict[str, str]:
        candidate = self._experience_candidate(experience_candidate_id)
        if candidate["status"] != "preparing" or not reason.strip():
            raise StateTransitionError("experience candidate failure needs an open candidate and a reason")
        failure = {
            "reason": reason.strip(),
            "model_run_id": model_run_id,
            "raw_model_output": raw_model_output,
            "raw_model_output_status": "available" if raw_model_output is not None else "not_available",
            "automatic_retry": False,
        }
        with self.conn:
            self.conn.execute(
                "UPDATE stage0_experience_candidate SET status='failed', failure_json=? WHERE experience_candidate_id=?",
                (_canonical(failure), experience_candidate_id),
            )
            self._audit(str(candidate["task_id"]), "experience_candidate_failed", {"experience_candidate_id": experience_candidate_id, **failure})
        return {"experience_candidate_id": experience_candidate_id, "status": "failed"}

    def decide_experience_candidate(
        self, *, experience_candidate_id: str, decision: str, actor: str, actor_kind: str, reason: str
    ) -> dict[str, str]:
        candidate = self._experience_candidate(experience_candidate_id)
        if candidate["status"] != "awaiting_human_decision" or decision not in {"accepted", "rejected"}:
            raise StateTransitionError("experience candidate is not awaiting an accept or reject decision")
        if actor_kind != "user" or not actor.strip() or not reason.strip():
            raise StateTransitionError("experience candidate decisions require an explicit user and reason")
        proposal = json.loads(str(candidate["proposal_json"] or "{}"))
        result = {"experience_candidate_id": experience_candidate_id, "decision": decision}
        with self.conn:
            if decision == "accepted":
                body = proposal["candidate"]
                experience_id = _id("confirmed_experience")
                sources = list(proposal["source_ids"])
                self.conn.execute(
                    "INSERT INTO stage0_confirmed_experience VALUES (?, ?, ?, 'shared_pattern', ?, ?, ?, ?, ?, 'active', ?, ?, ?)",
                    (experience_id, experience_candidate_id, candidate["domain_label"], body["summary"], _canonical(body["applicable_when"]), _canonical(body["method"]), _canonical(sources), _canonical(body["boundary"]), self.data_identity, actor.strip(), _now()),
                )
                result["experience_id"] = experience_id
            self.conn.execute(
                "UPDATE stage0_experience_candidate SET status=?, decided_by=?, decided_at=?, decision_reason=? WHERE experience_candidate_id=?",
                (decision, actor.strip(), _now(), reason.strip(), experience_candidate_id),
            )
            self._audit(str(candidate["task_id"]), "experience_candidate_decided", result)
        return result

    def list_active_experiences(
        self, *, domain_label: str, context_text: str | None = None, limit: int | None = None
    ) -> list[dict[str, Any]]:
        rows = self.conn.execute(
            "SELECT * FROM stage0_confirmed_experience WHERE domain_label=? AND status='active' AND data_identity=? ORDER BY confirmed_at, experience_id",
            (domain_label, self.data_identity),
        ).fetchall()
        experiences: list[dict[str, Any]] = []
        for row in rows:
            applicable_when = json.loads(str(row["applicable_when_json"]))
            explicit_context_match = (
                _experience_context_matches(
                    context_text=context_text, applicable_when=applicable_when
                )
                if context_text is not None
                else []
            )
            if context_text is not None and not explicit_context_match:
                continue
            experiences.append({
                "experience_id": str(row["experience_id"]), "classification": str(row["classification"]),
                "summary": str(row["summary"]), "applicable_when": applicable_when,
                "method": json.loads(str(row["method_json"])), "source_ids": json.loads(str(row["source_refs_json"])),
                "boundary": json.loads(str(row["boundary_json"])),
                "explicit_context_match": explicit_context_match,
            })
            if limit is not None and len(experiences) >= limit:
                break
        return experiences

    def list_task_experience_candidates(self, *, task_id: str) -> list[dict[str, Any]]:
        rows = self.conn.execute(
            "SELECT * FROM stage0_experience_candidate WHERE task_id=? AND data_identity=? ORDER BY created_at, experience_candidate_id",
            (task_id, self.data_identity),
        ).fetchall()
        return [
            {
                "experience_candidate_id": str(row["experience_candidate_id"]), "status": str(row["status"]),
                "proposal": json.loads(str(row["proposal_json"] or "{}")), "failure": json.loads(str(row["failure_json"])),
                "source_count": len(json.loads(str(row["frozen_sources_json"]))),
            }
            for row in rows
        ]

    def _experience_candidate(self, experience_candidate_id: str) -> sqlite3.Row:
        row = self.conn.execute(
            "SELECT * FROM stage0_experience_candidate WHERE experience_candidate_id=? AND data_identity=?",
            (experience_candidate_id, self.data_identity),
        ).fetchone()
        if row is None:
            raise StateTransitionError("experience candidate does not exist in this data identity")
        return row

    def list_content_workbench(self) -> list[dict[str, Any]]:
        rows = self.conn.execute(
            "SELECT * FROM stage0_content_task WHERE data_identity=? "
            "ORDER BY created_at DESC, task_id DESC",
            (self.data_identity,),
        ).fetchall()
        results: list[dict[str, Any]] = []
        for row in rows:
            topic = self.get_artifact_payload(str(row["topic_version_id"]))
            current = None
            if row["current_version_id"]:
                try:
                    current = self.get_artifact_payload(
                        str(row["current_version_id"])
                    )
                except StateTransitionError:
                    current = None
            failure_row = (
                self.conn.execute(
                    "SELECT failure_stage, reason, raw_model_output, raw_model_output_status, created_at "
                    "FROM stage0_content_node_failure WHERE failed_version_id=? AND data_identity=?",
                    (row["current_version_id"], self.data_identity),
                ).fetchone()
                if row["current_version_id"]
                else None
            )
            materials = self.conn.execute(
                "SELECT research_material_id, source_ref, title, evidence_role, "
                "material_json, integrity_hash, collected_at "
                "FROM stage0_research_material "
                "WHERE task_id=? AND data_identity=? "
                "ORDER BY collected_at, research_material_id",
                (row["task_id"], self.data_identity),
            ).fetchall()
            audio_attempts = self.conn.execute(
                "SELECT * FROM stage0_audio_production "
                "WHERE task_id=? AND data_identity=? "
                "ORDER BY attempt_number DESC, created_at DESC",
                (row["task_id"], self.data_identity),
            ).fetchall()
            results.append(
                {
                    "task_id": str(row["task_id"]),
                    "current_node": str(row["current_node"]),
                    "current_status": str(row["current_status"]),
                    "created_at": str(row["created_at"]),
                    "topic": topic,
                    "current_artifact": current,
                    "failure": (
                        {
                            "failure_stage": str(failure_row["failure_stage"]),
                            "reason": str(failure_row["reason"]),
                            "raw_model_output": failure_row["raw_model_output"],
                            "raw_model_output_status": str(failure_row["raw_model_output_status"]),
                            "created_at": str(failure_row["created_at"]),
                        }
                        if failure_row is not None
                        else None
                    ),
                    "experience_candidates": self.list_task_experience_candidates(task_id=str(row["task_id"])),
                    "research_materials": [
                        {
                            "research_material_id": str(material["research_material_id"]),
                            "source_ref": str(material["source_ref"]),
                            "title": str(material["title"]),
                            "evidence_role": str(material["evidence_role"]),
                            "material": json.loads(str(material["material_json"])),
                            "integrity_hash": str(material["integrity_hash"]),
                            "collected_at": str(material["collected_at"]),
                        }
                        for material in materials
                    ],
                    "audio_attempts": [
                        {
                            **{
                                key: attempt[key]
                                for key in attempt.keys()
                                if key
                                not in {
                                    "synthesis_json",
                                    "quality_review_json",
                                    "failure_json",
                                }
                            },
                            "synthesis": json.loads(str(attempt["synthesis_json"])),
                            "quality_review": json.loads(
                                str(attempt["quality_review_json"])
                            ),
                            "failure": json.loads(str(attempt["failure_json"])),
                        }
                        for attempt in audio_attempts
                    ],
                }
            )
        return results

    def register_content_account(
        self,
        *,
        content_account_id: str,
        account_role: str,
        display_name: str,
        domain_label: str,
        external_account_ref: str | None,
        actor: str,
    ) -> dict[str, str]:
        """Register an owned or competitor account as a formal content subject."""
        if account_role not in CONTENT_ACCOUNT_ROLES:
            raise StateTransitionError("content account role is not supported")
        if domain_label not in formal_domain_labels():
            raise StateTransitionError("content account requires a configured formal domain")
        if not content_account_id.strip() or not display_name.strip() or not actor.strip():
            raise StateTransitionError("content account requires an identity, display name and actor")
        if account_role == "competitor" and not str(external_account_ref or "").strip():
            raise StateTransitionError("competitor account requires its external account reference")
        now = _now()
        with self.conn:
            self.conn.execute(
                """
                INSERT INTO stage0_content_account(
                    content_account_id, account_role, display_name, domain_label, external_account_ref,
                    status, data_identity, created_by, created_at
                ) VALUES (?, ?, ?, ?, ?, 'active', ?, ?, ?)
                """,
                (
                    content_account_id.strip(), account_role, display_name.strip(), domain_label,
                    str(external_account_ref).strip() if external_account_ref else None,
                    self.data_identity, actor.strip(), now,
                ),
            )
        return {"content_account_id": content_account_id.strip(), "account_role": account_role, "created_at": now}

    def configure_cold_start_subjects(
        self,
        *,
        confirmation_key: str,
        domain_mode: str,
        domain_label: str,
        domain_name: str,
        domain_boundary: str,
        platform: str,
        owned_account: dict[str, str],
        competitor_accounts: tuple[dict[str, str], ...],
        actor: str,
    ) -> dict[str, Any]:
        """Confirm one complete cold-start configuration through the formal Core entry."""
        required = (
            confirmation_key, domain_mode, domain_label, domain_name, domain_boundary,
            platform, actor, str(owned_account.get("display_name") or ""),
            str(owned_account.get("external_account_ref") or ""),
        )
        if not all(str(value).strip() for value in required):
            raise StateTransitionError("cold-start configuration requires domain, owned account, platform and actor")
        if domain_mode not in {"reuse", "create"}:
            raise StateTransitionError("cold-start domain mode must be reuse or create")
        if domain_label not in formal_domain_labels():
            raise StateTransitionError("cold-start configuration requires a configured formal domain")
        existing = self.conn.execute(
            "SELECT configuration_id FROM stage0_cold_start_configuration WHERE confirmation_key=? AND data_identity=?",
            (confirmation_key.strip(), self.data_identity),
        ).fetchone()
        if existing is not None:
            return self.get_cold_start_configuration(configuration_id=existing["configuration_id"])
        if not (
            COLD_START_COMPETITOR_MIN
            <= len(competitor_accounts)
            <= COLD_START_COMPETITOR_MAX
        ):
            raise StateTransitionError(
                "cold-start configuration requires between 10 and 20 competitor accounts"
            )
        competitor_refs = [str(item.get("external_account_ref") or "").strip() for item in competitor_accounts]
        competitor_names = [str(item.get("display_name") or "").strip() for item in competitor_accounts]
        owned_ref = str(owned_account.get("external_account_ref") or "").strip()
        if not all(competitor_refs) or not all(competitor_names):
            raise StateTransitionError("every competitor account requires a name and platform identity")
        if len(set(competitor_refs)) != len(competitor_refs) or owned_ref in set(competitor_refs):
            raise StateTransitionError("owned and competitor account identities must be distinct")

        def ensure_account(role: str, display_name: str, external_ref: str) -> str:
            row = self.conn.execute(
                "SELECT content_account_id FROM stage0_content_account WHERE account_role=? AND domain_label=? "
                "AND external_account_ref=? AND data_identity=?",
                (role, domain_label, external_ref, self.data_identity),
            ).fetchone()
            if row is not None:
                return str(row["content_account_id"])
            account_id = _id(f"{role}_account")
            self.conn.execute(
                "INSERT INTO stage0_content_account(content_account_id, account_role, display_name, domain_label, "
                "external_account_ref, status, data_identity, created_by, created_at) "
                "VALUES (?, ?, ?, ?, ?, 'active', ?, ?, ?)",
                (account_id, role, display_name, domain_label, external_ref, self.data_identity, actor.strip(), _now()),
            )
            return account_id

        configuration_id = "cold_start_configuration_" + hashlib.sha256(
            f"{self.data_identity}:{confirmation_key.strip()}".encode("utf-8")
        ).hexdigest()[:24]
        now = _now()
        with self.conn:
            owned_account_id = ensure_account(
                "owned", str(owned_account["display_name"]).strip(), owned_ref,
            )
            competitor_account_ids = [
                ensure_account("competitor", name, ref)
                for name, ref in zip(competitor_names, competitor_refs)
            ]
            self.conn.execute(
                "INSERT INTO stage0_cold_start_configuration(configuration_id, confirmation_key, domain_mode, "
                "domain_label, domain_name, domain_boundary, platform, owned_account_id, "
                "competitor_account_ids_json, status, data_identity, confirmed_by, confirmed_at, cold_start_id) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, 'confirmed', ?, ?, ?, NULL)",
                (
                    configuration_id, confirmation_key.strip(), domain_mode, domain_label,
                    domain_name.strip(), domain_boundary.strip(), platform.strip(), owned_account_id,
                    _canonical(competitor_account_ids), self.data_identity, actor.strip(), now,
                ),
            )
            self._audit(None, "cold_start_configuration_confirmed_by_user", {
                "configuration_id": configuration_id, "domain_mode": domain_mode,
                "domain_label": domain_label, "owned_account_id": owned_account_id,
                "competitor_account_ids": competitor_account_ids,
            })
        return self.get_cold_start_configuration(configuration_id=configuration_id)

    def get_cold_start_configuration(self, *, configuration_id: str) -> dict[str, Any]:
        row = self.conn.execute(
            "SELECT * FROM stage0_cold_start_configuration WHERE configuration_id=? AND data_identity=?",
            (configuration_id, self.data_identity),
        ).fetchone()
        if row is None:
            raise StateTransitionError("cold-start configuration does not exist in this data identity")
        result = {key: row[key] for key in row.keys()}
        result["competitor_account_ids"] = json.loads(result.pop("competitor_account_ids_json"))
        account_ids = [result["owned_account_id"], *result["competitor_account_ids"]]
        result["accounts"] = [
            {key: account[key] for key in account.keys()}
            for account_id in account_ids
            for account in [self.conn.execute(
                "SELECT * FROM stage0_content_account WHERE content_account_id=? AND data_identity=?",
                (account_id, self.data_identity),
            ).fetchone()]
            if account is not None
        ]
        return result

    def list_cold_start_configurations(self) -> list[dict[str, Any]]:
        rows = self.conn.execute(
            "SELECT configuration_id FROM stage0_cold_start_configuration WHERE data_identity=? "
            "ORDER BY confirmed_at DESC, configuration_id DESC",
            (self.data_identity,),
        ).fetchall()
        return [self.get_cold_start_configuration(configuration_id=row["configuration_id"]) for row in rows]

    def start_configured_cold_start(
        self,
        *,
        configuration_id: str,
        actor: str,
        idempotency_key: str,
        preflight_receipt_id: str,
    ) -> dict[str, Any]:
        """Consume one confirmed configuration and queue every competitor registration."""
        configuration = self.get_cold_start_configuration(configuration_id=configuration_id)
        cold_start_id = str(configuration.get("cold_start_id") or f"cold_start_{configuration_id}")
        result = self.start_cold_start(
            cold_start_id=cold_start_id,
            owned_account_id=configuration["owned_account_id"],
            competitor_account_ids=tuple(configuration["competitor_account_ids"]),
            actor=actor,
            idempotency_key=idempotency_key,
            preflight_receipt_id=preflight_receipt_id,
        )
        with self.conn:
            self.conn.execute(
                "UPDATE stage0_cold_start_configuration SET status='started', cold_start_id=? "
                "WHERE configuration_id=? AND data_identity=? AND status IN ('confirmed', 'started')",
                (cold_start_id, configuration_id, self.data_identity),
            )
            self._audit(None, "configured_cold_start_started", {
                "configuration_id": configuration_id, "cold_start_id": cold_start_id,
                "registration_ids": result["registration_ids"],
            })
        return {**result, "configuration_id": configuration_id}

    def pause_configured_cold_start(
        self,
        *,
        configuration_id: str,
        actor: str,
        reason: str,
        idempotency_key: str,
    ) -> dict[str, Any]:
        """Pause external execution while preserving resumable formal progress."""
        if not actor.strip() or not reason.strip() or not idempotency_key.strip():
            raise StateTransitionError("pausing a configured cold start requires an actor, reason and idempotency key")
        configuration = self.get_cold_start_configuration(configuration_id=configuration_id)
        request = {
            "configuration_id": configuration_id,
            "actor": actor.strip(),
            "reason": reason.strip(),
        }
        replay = self._replay("pause_configured_cold_start", idempotency_key, request)
        if replay:
            return replay
        if configuration["status"] != "started" or not str(configuration.get("cold_start_id") or "").strip():
            raise StateTransitionError("only a started configured cold start can be paused")
        result = {
            "configuration_id": configuration_id,
            "cold_start_id": str(configuration["cold_start_id"]),
            "status": "confirmed",
            "resumable": True,
        }
        with self.conn:
            self.conn.execute(
                "UPDATE stage0_cold_start_configuration SET status='confirmed' "
                "WHERE configuration_id=? AND data_identity=? AND status='started'",
                (configuration_id, self.data_identity),
            )
            self._audit(None, "configured_cold_start_paused", {
                **result,
                "actor": actor.strip(),
                "reason": reason.strip(),
            })
            self._receipt("pause_configured_cold_start", idempotency_key, request, result)
        return result

    def record_live_cold_start_preflight(
        self,
        *,
        domain_label: str,
        owned_account_id: str,
        competitor_account_ids: tuple[str, ...],
        voice_profile_id: str,
        report: dict[str, Any],
    ) -> dict[str, str]:
        """Issue a short-lived receipt only for a machine report whose every required check passed."""
        checks = report.get("checks")
        if (
            self.data_identity != "production" or report.get("ready") is not True
            or report.get("domain_label") != domain_label or not isinstance(checks, list) or not checks
            or any(
                not isinstance(item, dict)
                or (
                    item.get("required_for_cold_start") is not False
                    and item.get("passed") is not True
                )
                for item in checks
            )
        ):
            raise StateTransitionError("live cold-start preflight receipt requires a complete passing production report")
        if not owned_account_id.strip() or not competitor_account_ids:
            raise StateTransitionError("live cold-start preflight receipt requires all cold-start subjects")
        receipt_id, inspected_at = _id("live_preflight"), _now()
        with self.conn:
            self.conn.execute(
                "INSERT INTO stage0_live_cold_start_preflight VALUES (?, ?, ?, ?, ?, ?, ?, 'ready', NULL, ?, ?)",
                (
                    receipt_id, domain_label, owned_account_id,
                    _canonical(list(competitor_account_ids)), voice_profile_id,
                    _canonical(report), _hash(report), self.data_identity, inspected_at,
                ),
            )
            self._audit(None, "live_cold_start_preflight_ready", {
                "preflight_receipt_id": receipt_id, "domain_label": domain_label,
            })
        return {"preflight_receipt_id": receipt_id, "status": "ready", "inspected_at": inspected_at}

    def start_cold_start(
        self,
        *,
        cold_start_id: str,
        owned_account_id: str,
        competitor_account_ids: tuple[str, ...],
        actor: str,
        idempotency_key: str,
        preflight_receipt_id: str | None = None,
    ) -> dict[str, Any]:
        """Start all required competitor registrations from one owned-account and domain decision."""
        if not cold_start_id.strip() or not actor.strip() or not competitor_account_ids:
            raise StateTransitionError("cold start requires an owned account, at least one competitor and an actor")
        if len(set(competitor_account_ids)) != len(competitor_account_ids):
            raise StateTransitionError("cold start competitor accounts must be distinct")
        owned = self.conn.execute(
            "SELECT * FROM stage0_content_account WHERE content_account_id=? AND data_identity=?", (owned_account_id, self.data_identity)
        ).fetchone()
        if owned is None or owned["account_role"] != "owned" or owned["status"] != "active":
            raise StateTransitionError("cold start requires an active owned account")
        competitors = [
            self.conn.execute(
                "SELECT * FROM stage0_content_account WHERE content_account_id=? AND data_identity=?", (account_id, self.data_identity)
            ).fetchone()
            for account_id in competitor_account_ids
        ]
        if any(row is None or row["account_role"] != "competitor" or row["status"] != "active" for row in competitors):
            raise StateTransitionError("cold start requires active registered competitor accounts")
        if any(str(row["domain_label"]) != str(owned["domain_label"]) for row in competitors if row is not None):
            raise StateTransitionError("cold start accounts must belong to one domain")
        preflight = None
        if self.data_identity == "production":
            preflight = self.conn.execute(
                "SELECT * FROM stage0_live_cold_start_preflight WHERE preflight_receipt_id=? AND data_identity=?",
                (str(preflight_receipt_id or ""), self.data_identity),
            ).fetchone()
            if (
                preflight is None or (
                    preflight["status"] != "ready"
                    and not (preflight["status"] == "consumed" and preflight["consumed_by_cold_start_id"] == cold_start_id)
                )
                or preflight["domain_label"] != owned["domain_label"]
                or preflight["owned_account_id"] != owned_account_id
                or json.loads(preflight["competitor_account_ids_json"]) != list(competitor_account_ids)
            ):
                raise StateTransitionError("production cold start requires a matching unused passing live preflight receipt")
            try:
                inspected_at = datetime.fromisoformat(str(preflight["inspected_at"]).replace("Z", "+00:00"))
            except ValueError as exc:
                raise StateTransitionError("live preflight receipt time is invalid") from exc
            if datetime.now(timezone.utc) - inspected_at > timedelta(minutes=15):
                raise StateTransitionError("live preflight receipt expired; all real connectors must be checked again")
        request = {"cold_start_id": cold_start_id, "owned_account_id": owned_account_id, "competitor_account_ids": list(competitor_account_ids), "actor": actor}
        replay = self._replay("start_cold_start", idempotency_key, request)
        if replay:
            return replay
        now = _now()
        registration_ids = [_id("competitor_registration") for _ in competitor_account_ids]
        with self.conn:
            self.conn.execute(
                """
                INSERT INTO stage0_cold_start(
                    cold_start_id, owned_account_id, domain_label, status, data_identity, created_by, created_at
                ) VALUES (?, ?, ?, 'registering_competitors', ?, ?, ?)
                """,
                (cold_start_id, owned_account_id, owned["domain_label"], self.data_identity, actor, now),
            )
            if preflight is not None and preflight["status"] == "ready":
                self.conn.execute(
                    "UPDATE stage0_live_cold_start_preflight SET status='consumed', consumed_by_cold_start_id=? WHERE preflight_receipt_id=?",
                    (cold_start_id, preflight["preflight_receipt_id"]),
                )
            for registration_id, competitor_id in zip(registration_ids, competitor_account_ids):
                self.conn.execute(
                    """
                    INSERT INTO stage0_competitor_registration(
                        registration_id, cold_start_id, competitor_account_id, current_step, status, data_identity, created_at
                    ) VALUES (?, ?, ?, 'historical_material', 'processing', ?, ?)
                    """,
                    (registration_id, cold_start_id, competitor_id, self.data_identity, now),
                )
            result = {"cold_start_id": cold_start_id, "registration_ids": registration_ids, "status": "registering_competitors"}
            self._receipt("start_cold_start", idempotency_key, request, result)
            self._audit(None, "cold_start_started", result)
        return result

    def record_competitor_registration_step(
        self,
        *,
        registration_id: str,
        step_name: str,
        artifact_refs: tuple[dict[str, Any], ...],
        actor: str,
        idempotency_key: str,
    ) -> dict[str, str]:
        """Record one completed required registration step; steps cannot be skipped or overwritten."""
        if step_name not in COMPETITOR_REGISTRATION_STEPS or not artifact_refs or not actor.strip():
            raise StateTransitionError("registration step requires its expected name, formal artifacts and actor")
        registration = self.conn.execute(
            "SELECT * FROM stage0_competitor_registration WHERE registration_id=? AND data_identity=?", (registration_id, self.data_identity)
        ).fetchone()
        if registration is None or registration["status"] != "processing" or registration["current_step"] != step_name:
            raise StateTransitionError("registration step is not the next required step")
        request = {"registration_id": registration_id, "step_name": step_name, "artifact_refs": list(artifact_refs), "actor": actor}
        replay = self._replay("record_competitor_registration_step", idempotency_key, request)
        if replay:
            return replay
        if step_name == "historical_material":
            if len(artifact_refs) != 1:
                raise StateTransitionError("historical collection must contain one formal artifact")
            try:
                validate_historical_collection_artifact(artifact_refs[0])
            except ValueError as exc:
                record_runtime_guard_event(
                    event="formal_historical_collection_write",
                    outcome="blocked",
                    details={"registration_id": registration_id, "error": str(exc)},
                )
                raise StateTransitionError(
                    f"historical collection runtime guard rejected the result: {exc}"
                ) from exc
            record_runtime_guard_event(
                event="formal_historical_collection_write",
                outcome="passed",
                details={"registration_id": registration_id},
            )
        if step_name == "high_signal_identification":
            historical_row = self.conn.execute(
                "SELECT artifact_refs_json FROM stage0_competitor_registration_step "
                "WHERE registration_id=? AND step_name='historical_material' AND data_identity=?",
                (registration_id, self.data_identity),
            ).fetchone()
            if historical_row is None:
                raise StateTransitionError("high-signal screening lacks its historical collection")
            historical_payload = _hide_build_root_paths(json.loads(historical_row["artifact_refs_json"]))
            historical_artifacts = historical_payload.get("artifact_refs", [])
            if (
                len(historical_artifacts) != 1
                or not isinstance(historical_artifacts[0].get("items"), list)
                or len(artifact_refs) != 1
            ):
                raise StateTransitionError("high-signal screening inputs are incomplete")
            try:
                validate_high_signal_artifact(
                    historical_items=historical_artifacts[0]["items"],
                    artifact=artifact_refs[0],
                )
            except ValueError as exc:
                record_runtime_guard_event(
                    event="formal_high_signal_write",
                    outcome="blocked",
                    details={"registration_id": registration_id, "error": str(exc)},
                )
                raise StateTransitionError(f"high-signal runtime guard rejected the result: {exc}") from exc
            record_runtime_guard_event(
                event="formal_high_signal_write",
                outcome="passed",
                details={"registration_id": registration_id},
            )
        step_index = COMPETITOR_REGISTRATION_STEPS.index(step_name)
        next_step = (
            COMPETITOR_REGISTRATION_STEPS[step_index + 1]
            if step_index + 1 < len(COMPETITOR_REGISTRATION_STEPS)
            else "awaiting_human_review"
        )
        next_status = "awaiting_human_review" if next_step == "awaiting_human_review" else "processing"
        now = _now()
        payload = {"artifact_refs": list(artifact_refs)}
        if _contains_build_root_path(payload):
            raise StateTransitionError("registration artifacts must not point into the build root")
        with self.conn:
            self.conn.execute(
                """
                INSERT INTO stage0_competitor_registration_step(
                    step_record_id, registration_id, step_name, artifact_refs_json, integrity_hash,
                    completed_by, completed_at, data_identity
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (_id("competitor_registration_step"), registration_id, step_name, _canonical(payload), _hash(payload), actor, now, self.data_identity),
            )
            self.conn.execute(
                "UPDATE stage0_competitor_registration SET current_step=?, status=? WHERE registration_id=?",
                (next_step, next_status, registration_id),
            )
            result = {"registration_id": registration_id, "completed_step": step_name, "next_step": next_step, "status": next_status}
            self._receipt("record_competitor_registration_step", idempotency_key, request, result)
            self._audit(None, "competitor_registration_step_completed", result)
        return result

    def get_competitor_registration(self, *, registration_id: str) -> dict[str, Any]:
        row = self.conn.execute(
            "SELECT registration.*, account.display_name, account.domain_label, account.external_account_ref "
            "FROM stage0_competitor_registration registration "
            "JOIN stage0_content_account account ON account.content_account_id=registration.competitor_account_id "
            "WHERE registration.registration_id=? AND registration.data_identity=? AND account.data_identity=?",
            (registration_id, self.data_identity, self.data_identity),
        ).fetchone()
        if row is None:
            raise StateTransitionError("competitor registration does not exist in this data identity")
        return {key: row[key] for key in row.keys()}

    def list_competitor_registration_steps(self, *, registration_id: str) -> list[dict[str, Any]]:
        registration = self.get_competitor_registration(registration_id=registration_id)
        rows = self.conn.execute(
            "SELECT step_name, artifact_refs_json, integrity_hash, completed_by, completed_at "
            "FROM stage0_competitor_registration_step WHERE registration_id=? AND data_identity=? "
            "ORDER BY completed_at, step_record_id",
            (registration["registration_id"], self.data_identity),
        ).fetchall()
        return [
            {
                "step_name": row["step_name"],
                "artifact_refs": _hide_build_root_paths(json.loads(row["artifact_refs_json"]))["artifact_refs"],
                "integrity_hash": row["integrity_hash"],
                "completed_by": row["completed_by"],
                "completed_at": row["completed_at"],
            }
            for row in rows
        ]

    def list_competitor_registration_items(
        self, *, registration_id: str, step_name: str
    ) -> list[dict[str, Any]]:
        self.get_competitor_registration(registration_id=registration_id)
        rows = self.conn.execute(
            "SELECT * FROM stage0_competitor_registration_item "
            "WHERE registration_id=? AND step_name=? AND data_identity=? ORDER BY item_ref",
            (registration_id, step_name, self.data_identity),
        ).fetchall()
        return [
            {
                "item_ref": row["item_ref"],
                "status": row["status"],
                "artifact": _hide_build_root_paths(json.loads(row["artifact_json"])),
                "error": _hide_build_root_paths(json.loads(row["error_json"])),
                "attempt_count": row["attempt_count"],
                "updated_at": row["updated_at"],
            }
            for row in rows
        ]

    def record_competitor_registration_item(
        self,
        *,
        registration_id: str,
        step_name: str,
        item_ref: str,
        status: str,
        artifact: dict[str, Any] | None,
        error: dict[str, Any] | None,
    ) -> dict[str, Any]:
        registration = self.get_competitor_registration(registration_id=registration_id)
        allowed_current_steps = (
            {"transcripts_and_comments", "breakdown"}
            if step_name == "breakdown"
            else {step_name}
        )
        if (
            registration["status"] != "processing"
            or registration["current_step"] not in allowed_current_steps
        ):
            raise StateTransitionError("registration item belongs to a stale or inactive step")
        if step_name not in {"transcripts_and_comments", "breakdown"} or status not in {"completed", "failed"}:
            raise StateTransitionError("registration item has an unsupported step or status")
        if not item_ref.strip() or (status == "completed" and not artifact) or (status == "failed" and not error):
            raise StateTransitionError("registration item needs an identity and matching result")
        with self.conn:
            self.conn.execute(
                "INSERT INTO stage0_competitor_registration_item VALUES (?, ?, ?, ?, ?, ?, 1, ?, ?) "
                "ON CONFLICT(registration_id, step_name, item_ref) DO UPDATE SET "
                "status=excluded.status, artifact_json=excluded.artifact_json, error_json=excluded.error_json, "
                "attempt_count=stage0_competitor_registration_item.attempt_count+1, updated_at=excluded.updated_at",
                (
                    registration_id, step_name, item_ref.strip(), status, _canonical(artifact or {}),
                    _canonical(error or {}), self.data_identity, _now(),
                ),
            )
        return {"registration_id": registration_id, "step_name": step_name, "item_ref": item_ref, "status": status}

    def record_competitor_breakdown_attempt(
        self,
        *,
        registration_id: str,
        source_id: str,
        attempt_kind: str,
        outcome: str,
        reason: str = "",
        raw_model_output: str | None = None,
        raw_model_output_status: str,
        model_run_id: str | None = None,
    ) -> dict[str, str]:
        """Append one immutable delivery record for a source-bound breakdown call."""
        if attempt_kind not in {"initial", "post_batch_delivery_retry"}:
            raise StateTransitionError("breakdown attempt has an unsupported kind")
        if outcome not in {"completed", "delivery_interrupted", "failed"}:
            raise StateTransitionError("breakdown attempt has an unsupported outcome")
        if not source_id.strip() or not raw_model_output_status.strip():
            raise StateTransitionError("breakdown attempt needs a source and output status")
        material = self.conn.execute(
            "SELECT 1 FROM stage0_competitor_registration_item WHERE registration_id=? "
            "AND step_name='transcripts_and_comments' AND item_ref=? AND status='completed' AND data_identity=?",
            (registration_id, source_id.strip(), self.data_identity),
        ).fetchone()
        if material is None:
            raise StateTransitionError("breakdown attempt requires retained spoken material")
        attempt_id = _id("competitor_breakdown_attempt")
        with self.conn:
            self.conn.execute(
                "INSERT INTO stage0_competitor_breakdown_attempt VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (
                    attempt_id, registration_id, source_id.strip(), attempt_kind, outcome,
                    reason, raw_model_output, raw_model_output_status, model_run_id,
                    self.data_identity, _now(),
                ),
            )
        return {"breakdown_attempt_id": attempt_id, "outcome": outcome}

    def latest_competitor_breakdown_attempt(
        self,
        *,
        registration_id: str,
        source_id: str,
    ) -> dict[str, str] | None:
        row = self.conn.execute(
            "SELECT attempt_kind, outcome FROM stage0_competitor_breakdown_attempt "
            "WHERE registration_id=? AND source_id=? AND data_identity=? "
            "ORDER BY created_at DESC, breakdown_attempt_id DESC LIMIT 1",
            (registration_id, source_id, self.data_identity),
        ).fetchone()
        if row is None:
            return None
        return {"attempt_kind": str(row["attempt_kind"]), "outcome": str(row["outcome"])}

    def record_replenished_competitor_material_item(
        self,
        *,
        registration_id: str,
        item_ref: str,
        status: str,
        artifact: dict[str, Any] | None,
        error: dict[str, Any] | None,
    ) -> dict[str, Any]:
        """Write only a user-authorized replacement for an incorrectly excluded transcript.

        This deliberately does not reopen registration progression.  It cannot
        trigger breakdown work, and it cannot touch a normal completed or
        failed material item.
        """
        if status not in {"completed", "failed"}:
            raise StateTransitionError("replenished material has an unsupported status")
        if not item_ref.strip() or (status == "completed" and not artifact) or (status == "failed" and not error):
            raise StateTransitionError("replenished material needs an identity and matching result")
        prior = self.conn.execute(
            "SELECT status, error_json FROM stage0_competitor_registration_item "
            "WHERE registration_id=? AND step_name='transcripts_and_comments' AND item_ref=? AND data_identity=?",
            (registration_id, item_ref.strip(), self.data_identity),
        ).fetchone()
        if prior is None or str(prior["status"]) != "excluded":
            raise StateTransitionError("replenished material must replace only an excluded source")
        try:
            prior_error = json.loads(str(prior["error_json"] or "{}"))
        except (TypeError, ValueError, json.JSONDecodeError) as exc:
            raise StateTransitionError("excluded material has an invalid exclusion record") from exc
        if prior_error.get("disposition") not in {
            "excluded_by_user_cleanup", "repair_retry_authorized_by_user",
        }:
            raise StateTransitionError("replenished material is not an explicitly authorized recovery source")
        signal = self.conn.execute(
            "SELECT artifact_refs_json FROM stage0_competitor_registration_step "
            "WHERE registration_id=? AND step_name='high_signal_identification' AND data_identity=?",
            (registration_id, self.data_identity),
        ).fetchone()
        if signal is None:
            raise StateTransitionError("replenished material has no original high-signal selection evidence")
        payload = _hide_build_root_paths(json.loads(str(signal["artifact_refs_json"])))
        selected_ids = {
            str(item.get("source_id") or "")
            for artifact in payload.get("artifact_refs", [])
            if isinstance(artifact, dict)
            for item in artifact.get("selected_items", [])
            if isinstance(item, dict)
        }
        if item_ref.strip() not in selected_ids:
            raise StateTransitionError("replenished material is outside the original high-signal selection")
        with self.conn:
            self.conn.execute(
                "UPDATE stage0_competitor_registration_item "
                "SET status=?, artifact_json=?, error_json=?, attempt_count=attempt_count+1, updated_at=? "
                "WHERE registration_id=? AND step_name='transcripts_and_comments' AND item_ref=? AND data_identity=? AND status='excluded'",
                (
                    status, _canonical(artifact or {}), _canonical(error or {}), _now(),
                    registration_id, item_ref.strip(), self.data_identity,
                ),
            )
        return {"registration_id": registration_id, "step_name": "transcripts_and_comments", "item_ref": item_ref, "status": status}

    def record_independent_competitor_breakdown(
        self,
        *,
        registration_id: str,
        item_ref: str,
        status: str,
        artifact: dict[str, Any] | None,
        error: dict[str, Any] | None,
        automatic_delivery_retry: bool = False,
    ) -> dict[str, Any]:
        """Persist one source-bound breakdown after its original registration is closed."""
        if status not in {"completed", "failed"} or not item_ref.strip():
            raise StateTransitionError("independent breakdown needs a valid result and source")
        if (status == "completed" and not artifact) or (status == "failed" and not error):
            raise StateTransitionError("independent breakdown needs a matching result payload")
        material = self.conn.execute(
            "SELECT status FROM stage0_competitor_registration_item WHERE registration_id=? "
            "AND step_name='transcripts_and_comments' AND item_ref=? AND data_identity=?",
            (registration_id, item_ref.strip(), self.data_identity),
        ).fetchone()
        if material is None or str(material["status"]) != "completed":
            raise StateTransitionError("independent breakdown requires a completed spoken material")
        prior = self.conn.execute(
            "SELECT status, error_json FROM stage0_competitor_registration_item WHERE registration_id=? "
            "AND step_name='breakdown' AND item_ref=? AND data_identity=?",
            (registration_id, item_ref.strip(), self.data_identity),
        ).fetchone()
        if prior is not None and str(prior["status"]) != "excluded":
            prior_error = json.loads(str(prior["error_json"] or "{}"))
            if not (
                automatic_delivery_retry
                and str(prior["status"]) == "failed"
                and prior_error.get("retry_disposition") == "one_post_batch_delivery_retry_pending"
            ):
                raise StateTransitionError("independent breakdown may replace only a retired breakdown record")
        if prior is not None:
            prior_error = json.loads(str(prior["error_json"] or "{}"))
            allowed_dispositions = {
                "retired_by_user", "repair_retry_authorized_by_user",
            }
            if automatic_delivery_retry:
                allowed_dispositions.add("one_post_batch_delivery_retry_pending")
            if prior_error.get("disposition") not in allowed_dispositions and prior_error.get("retry_disposition") not in allowed_dispositions:
                raise StateTransitionError("independent breakdown cannot replace a non-retired record")
        with self.conn:
            self.conn.execute(
                "INSERT INTO stage0_competitor_registration_item VALUES (?, 'breakdown', ?, ?, ?, ?, 1, ?, ?) "
                "ON CONFLICT(registration_id, step_name, item_ref) DO UPDATE SET "
                "status=excluded.status, artifact_json=excluded.artifact_json, error_json=excluded.error_json, "
                "attempt_count=stage0_competitor_registration_item.attempt_count+1, updated_at=excluded.updated_at",
                (registration_id, item_ref.strip(), status, _canonical(artifact or {}), _canonical(error or {}), self.data_identity, _now()),
            )
        return {"registration_id": registration_id, "step_name": "breakdown", "item_ref": item_ref, "status": status}

    def record_human_quality_rejected_competitor_breakdown(
        self,
        *,
        registration_id: str,
        item_ref: str,
        reason: str,
        failure_stage: str = "human_quality_acceptance",
        failure_type: str = "HumanQualityRejected",
    ) -> dict[str, Any]:
        """Turn a human-rejected completed breakdown into a retained failure.

        The completed artifact remains byte-for-byte in place.  This is a status
        correction, never a replacement or deletion of the prior formal result.
        """
        if not item_ref.strip() or not reason.strip():
            raise StateTransitionError("quality rejection needs an item identity and reason")
        prior = self.conn.execute(
            "SELECT status, artifact_json FROM stage0_competitor_registration_item "
            "WHERE registration_id=? AND step_name='breakdown' AND item_ref=? AND data_identity=?",
            (registration_id, item_ref.strip(), self.data_identity),
        ).fetchone()
        if prior is None or str(prior["status"]) != "completed":
            raise StateTransitionError("quality rejection requires a currently completed breakdown")
        artifact_json = str(prior["artifact_json"])
        try:
            artifact = json.loads(artifact_json)
        except (TypeError, ValueError, json.JSONDecodeError) as exc:
            raise StateTransitionError("completed breakdown artifact cannot be preserved") from exc
        raw_output = artifact.get("raw_model_output") if isinstance(artifact, dict) else None
        error = {
            "reason": reason.strip(),
            "failure_stage": failure_stage,
            "failure_type": failure_type,
            "raw_model_output": raw_output if isinstance(raw_output, str) else None,
            "raw_model_output_status": "available" if isinstance(raw_output, str) else "not_available_legacy_completed_record",
            "automatic_retry": False,
        }
        with self.conn:
            self.conn.execute(
                "UPDATE stage0_competitor_registration_item "
                "SET status='failed', artifact_json=?, error_json=?, attempt_count=attempt_count+1, updated_at=? "
                "WHERE registration_id=? AND step_name='breakdown' AND item_ref=? AND data_identity=? AND status='completed'",
                (
                    artifact_json,
                    _canonical(error),
                    _now(),
                    registration_id,
                    item_ref.strip(),
                    self.data_identity,
                ),
            )
        return {"registration_id": registration_id, "step_name": "breakdown", "item_ref": item_ref, "status": "failed"}

    def record_replenished_competitor_material_collection_checkpoint(
        self,
        *,
        registration_id: str,
        item_ref: str,
        detail: dict[str, Any],
        comments: list[dict[str, Any]],
        collection_ref: str,
    ) -> dict[str, Any]:
        """Keep a returned platform detail before download or transcription begins.

        This is intentionally limited to the user-authorized replacement set.
        It makes a stopped repair resumable without turning the registration
        back on or scheduling any analysis.
        """
        source_id = item_ref.strip()
        if not source_id or str(detail.get("source_id") or "").strip() != source_id:
            raise StateTransitionError("collected detail must match its selected source")
        if not collection_ref.strip():
            raise StateTransitionError("collected detail needs its raw collection reference")
        if not isinstance(comments, list):
            raise StateTransitionError("collected comments must be a list")
        prior = self.conn.execute(
            "SELECT status, error_json FROM stage0_competitor_registration_item "
            "WHERE registration_id=? AND step_name='transcripts_and_comments' AND item_ref=? AND data_identity=?",
            (registration_id, source_id, self.data_identity),
        ).fetchone()
        if prior is None or str(prior["status"]) != "excluded":
            raise StateTransitionError("collected detail must belong to an excluded recovery source")
        try:
            prior_error = json.loads(str(prior["error_json"] or "{}"))
        except (TypeError, ValueError, json.JSONDecodeError) as exc:
            raise StateTransitionError("excluded material has an invalid exclusion record") from exc
        if prior_error.get("disposition") not in {
            "excluded_by_user_cleanup", "repair_retry_authorized_by_user",
        }:
            raise StateTransitionError("collected detail is not an explicitly authorized recovery source")
        signal = self.conn.execute(
            "SELECT artifact_refs_json FROM stage0_competitor_registration_step "
            "WHERE registration_id=? AND step_name='high_signal_identification' AND data_identity=?",
            (registration_id, self.data_identity),
        ).fetchone()
        if signal is None:
            raise StateTransitionError("collected detail has no original high-signal selection evidence")
        payload = _hide_build_root_paths(json.loads(str(signal["artifact_refs_json"])))
        selected_ids = {
            str(item.get("source_id") or "")
            for artifact in payload.get("artifact_refs", [])
            if isinstance(artifact, dict)
            for item in artifact.get("selected_items", [])
            if isinstance(item, dict)
        }
        if source_id not in selected_ids:
            raise StateTransitionError("collected detail is outside the original high-signal selection")
        with self.conn:
            self.conn.execute(
                "INSERT INTO stage0_competitor_material_collection_checkpoint VALUES (?, ?, ?, ?, ?, ?, ?) "
                "ON CONFLICT(registration_id, item_ref) DO UPDATE SET "
                "detail_json=excluded.detail_json, comments_json=excluded.comments_json, "
                "collection_ref=excluded.collection_ref, recorded_at=excluded.recorded_at",
                (
                    registration_id, source_id, _canonical(detail), _canonical(comments),
                    collection_ref.strip(), self.data_identity, _now(),
                ),
            )
        return {"registration_id": registration_id, "item_ref": source_id, "status": "collected"}

    def list_replenished_competitor_material_collection_checkpoints(
        self, *, registration_id: str
    ) -> list[dict[str, Any]]:
        self.get_competitor_registration(registration_id=registration_id)
        rows = self.conn.execute(
            "SELECT * FROM stage0_competitor_material_collection_checkpoint "
            "WHERE registration_id=? AND data_identity=? ORDER BY item_ref",
            (registration_id, self.data_identity),
        ).fetchall()
        return [
            {
                "item_ref": str(row["item_ref"]),
                "detail": json.loads(str(row["detail_json"])),
                "comments": json.loads(str(row["comments_json"])),
                "collection_ref": str(row["collection_ref"]),
                "recorded_at": str(row["recorded_at"]),
            }
            for row in rows
        ]

    def record_competitor_registration_tag_candidates(
        self,
        *,
        registration_id: str,
        candidates: tuple[dict[str, Any], ...],
        actor: str,
    ) -> tuple[dict[str, Any], ...]:
        registration = self.get_competitor_registration(registration_id=registration_id)
        if registration["status"] != "processing" or registration["current_step"] != "tag_candidates":
            raise StateTransitionError("registration tag candidates belong to a stale or inactive step")
        policy = get_discovery_policy(str(registration["domain_label"]))
        topic_policy = policy.get("topic_search") if isinstance(policy.get("topic_search"), dict) else {}
        generic_tags = {str(value).strip().casefold() for value in topic_policy.get("generic_tags", [])}
        activity_terms = tuple(str(value).strip() for value in topic_policy.get("activity_review_terms", []))
        retained: list[dict[str, Any]] = []
        now = _now()
        for candidate in candidates:
            tag = str(candidate.get("tag") or "").strip().lstrip("#")
            source_ids = candidate.get("source_ids")
            if not tag or tag.casefold() in generic_tags or not isinstance(source_ids, list) or not source_ids:
                continue
            active_activity = self.conn.execute(
                "SELECT 1 FROM domain_search_activity_tag_registry WHERE domain_label=? AND tag=? "
                "AND status='active' AND valid_from<=? AND valid_until>=? LIMIT 1",
                (registration["domain_label"], tag, now, now),
            ).fetchone()
            if active_activity is not None:
                continue
            status = "pending_review" if any(term and term in tag for term in activity_terms) else "suggested"
            tag_id = f"registration_tag_{_hash({'domain': registration['domain_label'], 'tag': tag})[:20]}"
            with self.conn:
                self.conn.execute(
                    "INSERT OR IGNORE INTO domain_search_tags("
                    "tag_id, tag, domain_label, status, source, source_video_id, human_review_status"
                    ") VALUES (?, ?, ?, ?, 'discovered', ?, 'pending_review')",
                    (tag_id, tag, registration["domain_label"], status, str(source_ids[0])),
                )
            retained.append({
                "tag_id": tag_id,
                "tag": tag,
                "status": status,
                "source_ids": [str(value) for value in source_ids],
                "reason": str(candidate.get("reason") or "").strip(),
                "human_confirmation_required": True,
                "recorded_by": actor,
            })
        return tuple(retained)

    def list_competitor_registration_tag_candidates(self, *, registration_id: str) -> list[dict[str, Any]]:
        registration = self.get_competitor_registration(registration_id=registration_id)
        step = next(
            (
                item for item in self.list_competitor_registration_steps(registration_id=registration_id)
                if item["step_name"] == "tag_candidates"
            ),
            None,
        )
        candidate_ids: list[str] = []
        if step is not None:
            for artifact in step["artifact_refs"]:
                if not isinstance(artifact, dict):
                    continue
                for candidate in artifact.get("candidates", []):
                    if isinstance(candidate, dict) and str(candidate.get("tag_id") or "").strip():
                        candidate_ids.append(str(candidate["tag_id"]))
        if not candidate_ids:
            return []
        rows = self.conn.execute(
            "SELECT tag_id, tag, status, human_review_status, source_video_id, reviewed_at, reviewed_note "
            "FROM domain_search_tags WHERE domain_label=? AND tag_id IN ("
            + ",".join("?" for _ in candidate_ids)
            + ") ORDER BY created_at, tag_id",
            (registration["domain_label"], *candidate_ids),
        ).fetchall()
        return [{key: row[key] for key in row.keys()} for row in rows]


    def build_cold_start_tag_library(
        self,
        *,
        cold_start_id: str,
        actor: str,
    ) -> dict[str, Any]:
        if not actor.strip():
            raise StateTransitionError("tag-library construction requires an actor")
        existing = self.conn.execute(
            "SELECT 1 FROM stage0_cold_start_tag_library WHERE cold_start_id=? AND data_identity=?",
            (cold_start_id, self.data_identity),
        ).fetchone()
        if existing is not None:
            return self.get_cold_start_tag_library(cold_start_id=cold_start_id)
        cold_start = self.conn.execute(
            "SELECT * FROM stage0_cold_start WHERE cold_start_id=? AND data_identity=?",
            (cold_start_id, self.data_identity),
        ).fetchone()
        if cold_start is None:
            raise StateTransitionError("cold start does not exist")
        registrations = self.conn.execute(
            "SELECT registration.registration_id, registration.competitor_account_id "
            "FROM stage0_competitor_registration registration "
            "WHERE registration.cold_start_id=? AND registration.data_identity=? "
            "ORDER BY registration.registration_id",
            (cold_start_id, self.data_identity),
        ).fetchall()
        if not registrations:
            raise StateTransitionError("the whole tag library requires registered competitor accounts")
        missing_high_signal = self.conn.execute(
            "SELECT 1 FROM stage0_competitor_registration registration "
            "WHERE registration.cold_start_id=? AND registration.data_identity=? "
            "AND NOT EXISTS ("
            "SELECT 1 FROM stage0_competitor_registration_step step "
            "WHERE step.registration_id=registration.registration_id "
            "AND step.data_identity=registration.data_identity "
            "AND step.step_name='high_signal_identification'"
            ") LIMIT 1",
            (cold_start_id, self.data_identity),
        ).fetchone()
        if missing_high_signal is not None:
            raise StateTransitionError(
                "the whole tag library can be built only after every account completes baseline and hit filtering"
            )

        raw_candidates: dict[str, dict[str, Any]] = {}
        source_item_ids: set[str] = set()
        source_author_names: set[str] = set()
        for registration in registrations:
            effective_steps = {
                item["step_name"]: item["artifact_refs"]
                for item in self.list_competitor_registration_steps(
                    registration_id=str(registration["registration_id"])
                )
            }
            artifacts = effective_steps.get("high_signal_identification")
            if artifacts is None:
                raise StateTransitionError("a completed registration lacks its high-signal source artifact")
            for artifact in artifacts if isinstance(artifacts, list) else []:
                if not isinstance(artifact, dict):
                    continue
                for item in artifact.get("selected_items", []):
                    if not isinstance(item, dict):
                        continue
                    source_id = str(item.get("source_id") or "").strip()
                    if not source_id:
                        continue
                    source_item_ids.add(source_id)
                    author = str(item.get("author") or "").strip().casefold()
                    if author:
                        source_author_names.add(author)
                    for raw_tag in _SOURCE_HASHTAG_PATTERN.findall(str(item.get("title") or "")):
                        tag = raw_tag.strip().strip(_TAG_EDGE_PUNCTUATION)
                        if not tag:
                            continue
                        key = tag.casefold()
                        candidate = raw_candidates.setdefault(
                            key,
                            {
                                "tag": tag,
                                "source_ids": [],
                                "registration_ids": [],
                            },
                        )
                        if source_id not in candidate["source_ids"]:
                            candidate["source_ids"].append(source_id)
                        registration_id = str(registration["registration_id"])
                        if registration_id not in candidate["registration_ids"]:
                            candidate["registration_ids"].append(registration_id)

        policy = get_discovery_policy(str(cold_start["domain_label"]))
        topic_policy = policy.get("topic_search") if isinstance(policy.get("topic_search"), dict) else {}
        topic_policy = {
            **topic_policy,
            "named_entity_terms": sorted({
                *(
                    str(value).strip()
                    for value in topic_policy.get("named_entity_terms", [])
                    if str(value).strip()
                ),
                *source_author_names,
            }),
        }
        generic = {
            str(value).strip().casefold()
            for value in (
                list(topic_policy.get("generic_tags", []))
                + list(topic_policy.get("broad_domain_tags", []))
            )
            if str(value).strip()
        }
        platform_terms = tuple(
            str(value).strip()
            for value in topic_policy.get("platform_generic_terms", [])
            if str(value).strip()
        )
        activity_terms = tuple(
            str(value).strip()
            for value in topic_policy.get("activity_review_terms", [])
            if str(value).strip()
        )
        support_floor = max(2, int(topic_policy.get("minimum_source_support_floor") or 2))
        support_ratio = max(0.0, float(topic_policy.get("minimum_source_support_ratio") or 0.0))
        minimum_support = max(support_floor, math.ceil(len(source_item_ids) * support_ratio))
        existing_rows = self.conn.execute(
            "SELECT tag FROM domain_search_tags WHERE domain_label=?",
            (cold_start["domain_label"],),
        ).fetchall()
        existing_tags = {str(row["tag"]).strip().casefold() for row in existing_rows}

        retained: list[dict[str, Any]] = []
        filtered: list[dict[str, Any]] = []
        for key, candidate in raw_candidates.items():
            source_count = len(candidate["source_ids"])
            reason = ""
            named_entity_reason = _deterministic_named_entity_filter_reason(
                str(candidate["tag"]),
                topic_policy=topic_policy,
            )
            if key in generic:
                reason = "平台通用或领域过宽"
            elif any(term in candidate["tag"] for term in platform_terms):
                reason = "平台套话"
            elif any(term in candidate["tag"] for term in activity_terms):
                reason = "限时活动"
            elif named_entity_reason:
                reason = named_entity_reason
            elif len(candidate["tag"]) > 24 or candidate["tag"].isdigit():
                reason = "不是稳定可复用的话题标签"
            elif source_count < minimum_support:
                reason = f"仅有{source_count}条高信号作品使用，尚不足以进入可复用话题库"
            elif key in existing_tags:
                reason = "已存在于领域标签库"
            enriched = {
                **candidate,
                "source_count": source_count,
                "account_count": len(candidate["registration_ids"]),
            }
            if reason:
                enriched["filter_reason"] = reason
                filtered.append(enriched)
            else:
                enriched["retention_reason"] = (
                    f"来自{source_count}条历史高信号作品，达到本轮重复证据门槛{minimum_support}条"
                )
                retained.append(enriched)
        retained.sort(
            key=lambda item: (
                -int(item["source_count"]),
                -int(item["account_count"]),
                str(item["tag"]).casefold(),
            )
        )
        filtered.sort(
            key=lambda item: (
                str(item["filter_reason"]),
                -int(item["source_count"]),
                str(item["tag"]).casefold(),
            )
        )

        tag_ids: list[str] = []
        now = _now()
        library_id = _id("cold_start_tag_library")
        with self.conn:
            for candidate in retained:
                tag_id = f"cold_start_tag_{_hash({'domain': cold_start['domain_label'], 'tag': str(candidate['tag']).casefold()})[:20]}"
                self.conn.execute(
                    "INSERT INTO domain_search_tags("
                    "tag_id, tag, domain_label, status, source, source_video_id, human_review_status"
                    ") VALUES (?, ?, ?, 'suggested', 'discovered', ?, 'pending_review')",
                    (
                        tag_id,
                        candidate["tag"],
                        cold_start["domain_label"],
                        candidate["source_ids"][0],
                    ),
                )
                candidate["tag_id"] = tag_id
                tag_ids.append(tag_id)
            candidate_set = {
                "rules": {
                    "source": "historical_high_signal_source_hashtags_only",
                    "generic_and_broad_tags": sorted(generic),
                    "platform_generic_terms": list(platform_terms),
                    "limited_activity_terms": list(activity_terms),
                    "named_entity_filter": (
                        "configured named entities plus retained source author names"
                    ),
                    "minimum_source_support": minimum_support,
                },
                "retained": retained,
                "filtered": filtered,
            }
            self.conn.execute(
                "INSERT INTO stage0_cold_start_tag_library VALUES (?, ?, ?, ?, ?, ?, ?, ?, "
                "'awaiting_human_review', ?, ?, ?, NULL, NULL, NULL)",
                (
                    library_id,
                    cold_start_id,
                    cold_start["domain_label"],
                    "source_hashtag_deterministic_v3",
                    len(source_item_ids),
                    minimum_support,
                    _canonical(candidate_set),
                    _canonical(tag_ids),
                    self.data_identity,
                    actor,
                    now,
                ),
            )
            self._audit(
                None,
                "cold_start_tag_library_built",
                {
                    "tag_library_id": library_id,
                    "cold_start_id": cold_start_id,
                    "source_item_count": len(source_item_ids),
                    "raw_unique_count": len(raw_candidates),
                    "retained_count": len(retained),
                    "filtered_count": len(filtered),
                    "minimum_source_support": minimum_support,
                    "model_used": False,
                },
            )
        return self.get_cold_start_tag_library(cold_start_id=cold_start_id)


    def get_cold_start_tag_library(self, *, cold_start_id: str) -> dict[str, Any] | None:
        row = self.conn.execute(
            "SELECT * FROM stage0_cold_start_tag_library WHERE cold_start_id=? AND data_identity=?",
            (cold_start_id, self.data_identity),
        ).fetchone()
        if row is None:
            return None
        candidate_set = json.loads(row["candidate_set_json"])
        candidates_by_id = {
            str(item.get("tag_id")): item
            for item in candidate_set.get("retained", [])
            if isinstance(item, dict) and item.get("tag_id")
        }
        tag_ids = json.loads(row["tag_ids_json"])
        tag_rows: dict[str, sqlite3.Row] = {}
        if tag_ids:
            placeholders = ",".join("?" for _ in tag_ids)
            rows = self.conn.execute(
                "SELECT tag_id, tag, status, human_review_status, source_video_id, reviewed_at, reviewed_note "
                f"FROM domain_search_tags WHERE tag_id IN ({placeholders}) ORDER BY created_at, tag_id",
                tuple(tag_ids),
            ).fetchall()
            tag_rows = {str(item["tag_id"]): item for item in rows}
        candidates: list[dict[str, Any]] = []
        for tag_id in tag_ids:
            tag_row = tag_rows.get(str(tag_id))
            evidence = candidates_by_id.get(str(tag_id), {})
            if tag_row is None:
                continue
            candidates.append({
                **{key: tag_row[key] for key in tag_row.keys()},
                "source_ids": list(evidence.get("source_ids", [])),
                "registration_ids": list(evidence.get("registration_ids", [])),
                "source_count": int(evidence.get("source_count") or 0),
                "account_count": int(evidence.get("account_count") or 0),
                "retention_reason": str(evidence.get("retention_reason") or ""),
            })
        return {
            "tag_library_id": row["tag_library_id"],
            "cold_start_id": cold_start_id,
            "domain_label": row["domain_label"],
            "status": row["status"],
            "extraction_method": row["extraction_method"],
            "source_item_count": int(row["source_item_count"]),
            "minimum_source_support": int(row["minimum_source_support"]),
            "raw_unique_count": len(candidate_set.get("retained", [])) + len(candidate_set.get("filtered", [])),
            "retained_count": len(candidate_set.get("retained", [])),
            "filtered_count": len(candidate_set.get("filtered", [])),
            "candidates": candidates,
            "reviewed_by": row["reviewed_by"],
            "reviewed_at": row["reviewed_at"],
            "review_reason": row["review_reason"],
        }

    def _hard_delete_domain_tag(self, *, tag_id: str) -> None:
        """Permanently remove one tag and every tag-owned search record."""
        raise StateTransitionError(
            "hard deletion of retained business tags and search records is retired"
        )
        libraries = self.conn.execute(
            "SELECT cold_start_id, candidate_set_json, tag_ids_json "
            "FROM stage0_cold_start_tag_library WHERE data_identity=?",
            (self.data_identity,),
        ).fetchall()
        for library in libraries:
            original_ids = [str(value) for value in json.loads(library["tag_ids_json"])]
            if tag_id not in original_ids:
                continue
            candidate_set = json.loads(library["candidate_set_json"])
            for key in ("retained", "filtered"):
                candidate_set[key] = [
                    item
                    for item in candidate_set.get(key, [])
                    if not isinstance(item, dict)
                    or str(item.get("tag_id") or "") != tag_id
                ]
            self.conn.execute(
                "UPDATE stage0_cold_start_tag_library SET candidate_set_json=?, tag_ids_json=? "
                "WHERE cold_start_id=? AND data_identity=?",
                (
                    _canonical(candidate_set),
                    _canonical([value for value in original_ids if value != tag_id]),
                    library["cold_start_id"],
                    self.data_identity,
                ),
            )
        self.conn.execute(
            "DELETE FROM discovered_external_videos WHERE tag_id=?",
            (tag_id,),
        )
        self.conn.execute(
            "DELETE FROM domain_search_page_observation WHERE tag_id=?",
            (tag_id,),
        )
        self.conn.execute(
            "DELETE FROM domain_search_cursor WHERE tag_id=?",
            (tag_id,),
        )
        self.conn.execute(
            "DELETE FROM domain_search_tags WHERE tag_id=?",
            (tag_id,),
        )

    def _hard_delete_cold_start_tag(self, *, cold_start_id: str, tag_id: str) -> None:
        row = self.conn.execute(
            "SELECT candidate_set_json, tag_ids_json FROM stage0_cold_start_tag_library "
            "WHERE cold_start_id=? AND data_identity=?",
            (cold_start_id, self.data_identity),
        ).fetchone()
        if row is None:
            raise StateTransitionError("the tag library does not exist")
        if tag_id not in {str(value) for value in json.loads(row["tag_ids_json"])}:
            raise StateTransitionError("the tag does not belong to this cold-start library")
        self._hard_delete_domain_tag(tag_id=tag_id)

    def delete_pending_cold_start_tag(
        self,
        *,
        cold_start_id: str,
        tag_id: str,
        actor: str,
        actor_kind: str,
        reason: str,
    ) -> dict[str, Any]:
        """Permanently remove one pending tag when the user clicks delete."""
        if actor_kind != "user" or not actor.strip() or not reason.strip():
            raise StateTransitionError("tag deletion requires an explicit user decision")
        library = self.get_cold_start_tag_library(cold_start_id=cold_start_id)
        if library is None or library["status"] != "awaiting_human_review":
            raise StateTransitionError("the tag library is not awaiting review")
        candidate = next(
            (
                item
                for item in library["candidates"]
                if str(item["tag_id"]) == tag_id
            ),
            None,
        )
        if candidate is None or candidate["human_review_status"] != "pending_review":
            raise StateTransitionError("the pending tag no longer exists")
        now = _now()
        with self.conn:
            self._hard_delete_cold_start_tag(cold_start_id=cold_start_id, tag_id=tag_id)
            result = {
                "cold_start_id": cold_start_id,
                "tag_id": tag_id,
                "tag": candidate["tag"],
                "status": "deleted",
                "deleted_at": now,
                "deleted_by": actor,
            }
            self._audit(None, "cold_start_tag_deleted_by_user", result)
        return result

    def revise_accepted_cold_start_tag_library(
        self,
        *,
        cold_start_id: str,
        deleted_tag_ids: tuple[str, ...],
        actor: str,
        actor_kind: str,
        reason: str,
    ) -> dict[str, Any]:
        """Apply one confirmed batch of deletions to an accepted tag library."""
        if actor_kind != "user" or not actor.strip() or not reason.strip():
            raise StateTransitionError("accepted tag-library revision requires an explicit user decision")
        normalized_ids = tuple(dict.fromkeys(
            str(tag_id).strip() for tag_id in deleted_tag_ids if str(tag_id).strip()
        ))
        if not normalized_ids:
            raise StateTransitionError("accepted tag-library revision requires at least one deletion")
        library = self.get_cold_start_tag_library(cold_start_id=cold_start_id)
        if library is None or library["status"] != "accepted":
            raise StateTransitionError("the tag library has not completed its first review")
        approved = {
            str(item["tag_id"]): item
            for item in library["candidates"]
            if item["status"] == "active" and item["human_review_status"] == "approved"
        }
        unknown_ids = set(normalized_ids) - set(approved)
        if unknown_ids:
            raise StateTransitionError("one or more selected tags are no longer active in this library")
        if len(approved) <= len(normalized_ids):
            raise StateTransitionError("an accepted tag library must keep at least one active tag")
        now = _now()
        deleted_items = [
            {"tag_id": tag_id, "tag": str(approved[tag_id]["tag"])}
            for tag_id in normalized_ids
        ]
        with self.conn:
            for tag_id in normalized_ids:
                self._hard_delete_cold_start_tag(cold_start_id=cold_start_id, tag_id=tag_id)
            self.conn.execute(
                "UPDATE stage0_cold_start_tag_library "
                "SET reviewed_by=?, reviewed_at=?, review_reason=? "
                "WHERE cold_start_id=? AND data_identity=? AND status='accepted'",
                (actor, now, reason, cold_start_id, self.data_identity),
            )
            result = {
                "tag_library_id": library["tag_library_id"],
                "cold_start_id": cold_start_id,
                "status": "accepted",
                "deleted_count": len(deleted_items),
                "remaining_count": len(approved) - len(deleted_items),
                "deleted_items": deleted_items,
                "reviewed_by": actor,
                "reviewed_at": now,
            }
            self._audit(None, "accepted_cold_start_tag_library_revised_by_user", result)
        return result

    def review_cold_start_tag_library(
        self,
        *,
        cold_start_id: str,
        decisions: tuple[dict[str, str], ...],
        actor: str,
        actor_kind: str,
        reason: str,
    ) -> dict[str, Any]:
        if actor_kind != "user" or not actor.strip() or not reason.strip():
            raise StateTransitionError("whole tag-library review requires an explicit user decision")
        library = self.get_cold_start_tag_library(cold_start_id=cold_start_id)
        if library is None or library["status"] != "awaiting_human_review":
            raise StateTransitionError("the whole tag library is not awaiting review")
        pending = {
            str(item["tag_id"]): item
            for item in library["candidates"]
            if item["human_review_status"] == "pending_review"
        }
        submitted: dict[str, dict[str, str]] = {}
        accepted_names: set[str] = set()
        for item in decisions:
            tag_id = str(item.get("tag_id") or "").strip()
            decision = str(item.get("decision") or "").strip()
            edited_tag = str(item.get("edited_tag") or "").strip().lstrip("#")
            if not tag_id or decision not in {"accepted", "rejected"}:
                raise StateTransitionError("every whole-library item must be kept or removed")
            if decision == "accepted" and not edited_tag:
                raise StateTransitionError("a kept tag cannot be empty")
            normalized = edited_tag.casefold()
            if decision == "accepted" and normalized in accepted_names:
                raise StateTransitionError("the reviewed tag library contains duplicate tags")
            if decision == "accepted":
                accepted_names.add(normalized)
            submitted[tag_id] = {
                "decision": decision,
                "edited_tag": edited_tag,
            }
        if set(submitted) != set(pending):
            raise StateTransitionError("the whole pending tag library must be reviewed in one submission")
        library_tag_ids = set(pending)
        if library_tag_ids:
            placeholders = ",".join("?" for _ in library_tag_ids)
            other_rows = self.conn.execute(
                "SELECT tag FROM domain_search_tags WHERE domain_label=? "
                f"AND tag_id NOT IN ({placeholders})",
                (library["domain_label"], *sorted(library_tag_ids)),
            ).fetchall()
        else:
            other_rows = self.conn.execute(
                "SELECT tag FROM domain_search_tags WHERE domain_label=?",
                (library["domain_label"],),
            ).fetchall()
        outside_names = {str(row["tag"]).strip().casefold() for row in other_rows}
        if accepted_names & outside_names:
            raise StateTransitionError("the reviewed tag library duplicates an existing domain tag")

        accepted = 0
        rejected = 0
        with self.conn:
            for tag_id in sorted(library_tag_ids):
                item = submitted[tag_id]
                original = pending[tag_id]
                if item["decision"] == "accepted":
                    accepted += 1
                    self.conn.execute(
                        "UPDATE domain_search_tags SET tag=?, status='active', human_review_status='approved', "
                        "reviewed_at=?, reviewed_note=? WHERE tag_id=? AND human_review_status='pending_review'",
                        (item["edited_tag"] or original["tag"], _now(), reason, tag_id),
                    )
                else:
                    rejected += 1
                    self._hard_delete_cold_start_tag(
                        cold_start_id=cold_start_id,
                        tag_id=tag_id,
                    )
            self.conn.execute(
                "UPDATE stage0_cold_start_tag_library SET status='accepted', reviewed_by=?, reviewed_at=?, "
                "review_reason=? WHERE cold_start_id=? AND data_identity=? AND status='awaiting_human_review'",
                (actor, _now(), reason, cold_start_id, self.data_identity),
            )
            result = {
                "tag_library_id": library["tag_library_id"],
                "cold_start_id": cold_start_id,
                "status": "accepted",
                "reviewed_count": len(pending),
                "accepted_count": accepted,
                "rejected_count": rejected,
            }
            self._audit(None, "cold_start_tag_library_reviewed_as_a_whole", result)
        return result

    def build_two_week_tag_library_review(
        self,
        *,
        domain_label: str,
        window_start: str,
        window_end: str,
        actor: str,
    ) -> dict[str, Any]:
        """Build one deterministic whole-library review without a model call."""
        if (
            domain_label not in formal_domain_labels()
            or not window_start.strip()
            or not window_end.strip()
            or not actor.strip()
        ):
            raise StateTransitionError(
                "two-week whole-library review requires a formal domain, window and actor"
            )
        existing = self.conn.execute(
            "SELECT tag_review_id FROM stage0_two_week_tag_library_review "
            "WHERE domain_label=? AND window_start=? AND window_end=? AND data_identity=?",
            (domain_label, window_start, window_end, self.data_identity),
        ).fetchone()
        if existing is not None:
            return self.get_two_week_tag_library_review(
                tag_review_id=str(existing["tag_review_id"])
            )
        completed = self.conn.execute(
            "SELECT 1 FROM stage0_cold_start_configuration "
            "WHERE domain_label=? AND status='completed' AND data_identity=? LIMIT 1",
            (domain_label, self.data_identity),
        ).fetchone()
        if completed is None:
            raise StateTransitionError(
                "two-week tag-library review requires a completed cold start"
            )
        hit_rows = self.conn.execute(
            "SELECT hit.hit_id, hit.platform_item_id, hit.title, account.account_name "
            "FROM hits hit JOIN competitor_accounts account ON account.account_id=hit.account_id "
            "WHERE account.domain_label=? AND hit.promoted_at>=? AND hit.promoted_at<? "
            "ORDER BY hit.promoted_at, hit.hit_id",
            (domain_label, window_start, window_end),
        ).fetchall()
        raw_candidates: dict[str, dict[str, Any]] = {}
        author_names = {
            str(row["account_name"] or "").strip().casefold()
            for row in hit_rows
            if str(row["account_name"] or "").strip()
        }
        for row in hit_rows:
            for raw_tag in _SOURCE_HASHTAG_PATTERN.findall(str(row["title"] or "")):
                tag = raw_tag.strip().strip(_TAG_EDGE_PUNCTUATION)
                if not tag:
                    continue
                candidate = raw_candidates.setdefault(
                    tag.casefold(),
                    {"tag": tag, "source_ids": [], "hit_ids": []},
                )
                source_id = str(row["platform_item_id"])
                hit_id = str(row["hit_id"])
                if source_id not in candidate["source_ids"]:
                    candidate["source_ids"].append(source_id)
                if hit_id not in candidate["hit_ids"]:
                    candidate["hit_ids"].append(hit_id)

        policy = get_discovery_policy(domain_label)
        topic_policy = (
            policy.get("topic_search")
            if isinstance(policy.get("topic_search"), dict)
            else {}
        )
        topic_policy = {
            **topic_policy,
            "named_entity_terms": sorted(
                {
                    *(
                        str(value).strip()
                        for value in topic_policy.get("named_entity_terms", [])
                        if str(value).strip()
                    ),
                    *author_names,
                }
            ),
        }
        generic = {
            str(value).strip().casefold()
            for value in (
                list(topic_policy.get("generic_tags", []))
                + list(topic_policy.get("broad_domain_tags", []))
            )
            if str(value).strip()
        }
        platform_terms = tuple(
            str(value).strip()
            for value in topic_policy.get("platform_generic_terms", [])
            if str(value).strip()
        )
        activity_terms = tuple(
            str(value).strip()
            for value in topic_policy.get("activity_review_terms", [])
            if str(value).strip()
        )
        support_floor = max(
            2, int(topic_policy.get("minimum_source_support_floor") or 2)
        )
        support_ratio = max(
            0.0, float(topic_policy.get("minimum_source_support_ratio") or 0.0)
        )
        minimum_support = max(
            support_floor, math.ceil(len(hit_rows) * support_ratio)
        )
        current_rows = self.conn.execute(
            "SELECT tag.tag_id, tag.tag, tag.status, tag.created_at, "
            "MAX(observation.observed_at) AS last_searched_at, "
            "COUNT(DISTINCT observation.run_id) AS search_count, "
            "COUNT(observation.observation_id) AS result_count, "
            "SUM(CASE WHEN observation.filter_outcome='eligible' THEN 1 ELSE 0 END) AS eligible_count "
            "FROM domain_search_tags tag "
            "LEFT JOIN domain_search_page_observation observation ON observation.tag_id=tag.tag_id "
            "WHERE tag.domain_label=? AND tag.status='active' "
            "AND tag.human_review_status='approved' "
            "GROUP BY tag.tag_id, tag.tag, tag.status, tag.created_at "
            "ORDER BY tag.created_at, tag.tag_id",
            (domain_label,),
        ).fetchall()
        current = [
            {
                "tag_id": str(row["tag_id"]),
                "tag": str(row["tag"]),
                "origin": "current_library",
                "created_at": row["created_at"],
                "last_searched_at": row["last_searched_at"],
                "search_count": int(row["search_count"] or 0),
                "result_count": int(row["result_count"] or 0),
                "eligible_result_count": int(row["eligible_count"] or 0),
            }
            for row in current_rows
        ]
        existing_names = {item["tag"].casefold() for item in current}
        retained: list[dict[str, Any]] = []
        filtered: list[dict[str, Any]] = []
        for key, candidate in raw_candidates.items():
            source_count = len(candidate["source_ids"])
            named_entity_reason = _deterministic_named_entity_filter_reason(
                str(candidate["tag"]), topic_policy=topic_policy
            )
            reason = ""
            if key in existing_names:
                reason = "已存在于领域标签库"
            elif key in generic:
                reason = "平台通用或领域过宽"
            elif any(term in candidate["tag"] for term in platform_terms):
                reason = "平台套话"
            elif any(term in candidate["tag"] for term in activity_terms):
                reason = "限时活动"
            elif named_entity_reason:
                reason = named_entity_reason
            elif len(candidate["tag"]) > 24 or candidate["tag"].isdigit():
                reason = "不是稳定可复用的话题标签"
            elif source_count < minimum_support:
                reason = (
                    f"仅有{source_count}条新爆款使用，"
                    f"尚不足本轮{minimum_support}条重复证据门槛"
                )
            enriched = {**candidate, "source_count": source_count}
            if reason:
                enriched["filter_reason"] = reason
                filtered.append(enriched)
            else:
                retained.append(enriched)
        retained.sort(key=lambda item: (-int(item["source_count"]), item["tag"].casefold()))
        filtered.sort(
            key=lambda item: (
                str(item["filter_reason"]),
                -int(item["source_count"]),
                item["tag"].casefold(),
            )
        )
        review_id = _id("two_week_tag_review")
        now = _now()
        suggested: list[dict[str, Any]] = []
        with self.conn:
            for candidate in retained:
                tag_id = (
                    "two_week_tag_"
                    + _hash(
                        {
                            "domain": domain_label,
                            "window_start": window_start,
                            "tag": str(candidate["tag"]).casefold(),
                        }
                    )[:20]
                )
                self.conn.execute(
                    "INSERT INTO domain_search_tags("
                    "tag_id, tag, domain_label, status, source, source_video_id, "
                    "human_review_status) VALUES (?, ?, ?, 'suggested', 'discovered', ?, 'pending_review')",
                    (
                        tag_id,
                        candidate["tag"],
                        domain_label,
                        candidate["source_ids"][0],
                    ),
                )
                suggested.append(
                    {**candidate, "tag_id": tag_id, "origin": "new_hit_hashtag"}
                )
            tag_ids = [
                *[item["tag_id"] for item in current],
                *[item["tag_id"] for item in suggested],
            ]
            candidate_set = {
                "rules": {
                    "model_calls": 0,
                    "source": "new_hit_source_hashtags_only",
                    "minimum_source_support": minimum_support,
                    "whole_library_review": True,
                    "automatic_pause_or_delete": False,
                },
                "current": current,
                "suggested": suggested,
                "filtered": filtered,
            }
            self.conn.execute(
                "INSERT INTO stage0_two_week_tag_library_review VALUES "
                "(?, ?, ?, ?, ?, ?, 'awaiting_human_review', ?, ?, ?, NULL, NULL, NULL)",
                (
                    review_id,
                    domain_label,
                    window_start,
                    window_end,
                    _canonical(candidate_set),
                    _canonical(tag_ids),
                    self.data_identity,
                    actor.strip(),
                    now,
                ),
            )
            self._audit(
                None,
                "two_week_tag_library_review_built_without_model",
                {
                    "tag_review_id": review_id,
                    "domain_label": domain_label,
                    "current_count": len(current),
                    "suggested_count": len(suggested),
                    "filtered_count": len(filtered),
                    "model_calls": 0,
                },
            )
        return self.get_two_week_tag_library_review(tag_review_id=review_id)

    def get_two_week_tag_library_review(
        self, *, tag_review_id: str
    ) -> dict[str, Any]:
        row = self.conn.execute(
            "SELECT * FROM stage0_two_week_tag_library_review "
            "WHERE tag_review_id=? AND data_identity=?",
            (tag_review_id, self.data_identity),
        ).fetchone()
        if row is None:
            raise StateTransitionError("two-week whole-library review does not exist")
        candidate_set = json.loads(row["candidate_set_json"])
        return {
            "tag_review_id": row["tag_review_id"],
            "domain_label": row["domain_label"],
            "window_start": row["window_start"],
            "window_end": row["window_end"],
            "status": row["status"],
            "model_calls": 0,
            "current": candidate_set.get("current", []),
            "suggested": candidate_set.get("suggested", []),
            "filtered": candidate_set.get("filtered", []),
            "created_at": row["created_at"],
            "reviewed_by": row["reviewed_by"],
            "reviewed_at": row["reviewed_at"],
            "review_reason": row["review_reason"],
        }

    def list_open_two_week_tag_library_reviews(
        self, *, domain_label: str | None = None
    ) -> list[dict[str, Any]]:
        if domain_label:
            rows = self.conn.execute(
                "SELECT tag_review_id FROM stage0_two_week_tag_library_review "
                "WHERE domain_label=? AND status='awaiting_human_review' "
                "AND data_identity=? ORDER BY window_end, tag_review_id",
                (domain_label, self.data_identity),
            ).fetchall()
        else:
            rows = self.conn.execute(
                "SELECT tag_review_id FROM stage0_two_week_tag_library_review "
                "WHERE status='awaiting_human_review' AND data_identity=? "
                "ORDER BY window_end, tag_review_id",
                (self.data_identity,),
            ).fetchall()
        return [
            self.get_two_week_tag_library_review(
                tag_review_id=str(row["tag_review_id"])
            )
            for row in rows
        ]

    def review_two_week_tag_library(
        self,
        *,
        tag_review_id: str,
        decisions: tuple[dict[str, Any], ...],
        actor: str,
        actor_kind: str,
        reason: str,
    ) -> dict[str, Any]:
        if actor_kind != "user" or not actor.strip() or not reason.strip():
            raise StateTransitionError(
                "two-week whole-library changes require an explicit user decision"
            )
        review = self.get_two_week_tag_library_review(tag_review_id=tag_review_id)
        if review["status"] != "awaiting_human_review":
            raise StateTransitionError("two-week whole-library review is not open")
        entries = {
            str(item["tag_id"]): item
            for item in [*review["current"], *review["suggested"]]
        }
        submitted: dict[str, dict[str, Any]] = {}
        kept_names: set[str] = set()
        for decision in decisions:
            tag_id = str(decision.get("tag_id") or "").strip()
            keep = decision.get("keep")
            final_tag = str(decision.get("edited_tag") or "").strip().lstrip("#")
            if tag_id not in entries or not isinstance(keep, bool):
                raise StateTransitionError(
                    "every whole-library entry must have one keep or delete decision"
                )
            if keep and not final_tag:
                final_tag = str(entries[tag_id]["tag"])
            normalized = final_tag.casefold()
            if keep and normalized in kept_names:
                raise StateTransitionError(
                    "the reviewed whole tag library contains duplicate tags"
                )
            if keep:
                kept_names.add(normalized)
            submitted[tag_id] = {
                "keep": keep,
                "edited_tag": final_tag,
            }
        if set(submitted) != set(entries):
            raise StateTransitionError(
                "the complete current and suggested tag library must be reviewed once"
            )
        deleted = 0
        kept = 0
        with self.conn:
            for tag_id, decision in submitted.items():
                if decision["keep"]:
                    duplicate = self.conn.execute(
                        "SELECT tag_id FROM domain_search_tags "
                        "WHERE domain_label=? AND lower(tag)=lower(?) AND tag_id!=? LIMIT 1",
                        (
                            review["domain_label"],
                            decision["edited_tag"],
                            tag_id,
                        ),
                    ).fetchone()
                    if duplicate is not None:
                        raise StateTransitionError(
                            "an edited tag duplicates another domain tag"
                        )
                    self.conn.execute(
                        "UPDATE domain_search_tags SET tag=?, status='active', "
                        "human_review_status='approved', reviewed_at=?, reviewed_note=? "
                        "WHERE tag_id=? AND domain_label=?",
                        (
                            decision["edited_tag"],
                            _now(),
                            reason.strip(),
                            tag_id,
                            review["domain_label"],
                        ),
                    )
                    kept += 1
                else:
                    self._hard_delete_domain_tag(tag_id=tag_id)
                    deleted += 1
            now = _now()
            self.conn.execute(
                "UPDATE stage0_two_week_tag_library_review "
                "SET status='accepted', reviewed_by=?, reviewed_at=?, review_reason=? "
                "WHERE tag_review_id=? AND data_identity=? "
                "AND status='awaiting_human_review'",
                (
                    actor.strip(),
                    now,
                    reason.strip(),
                    tag_review_id,
                    self.data_identity,
                ),
            )
            result = {
                "tag_review_id": tag_review_id,
                "status": "accepted",
                "kept_count": kept,
                "deleted_count": deleted,
                "reviewed_by": actor.strip(),
                "reviewed_at": now,
            }
            self._audit(None, "two_week_tag_library_reviewed_as_a_whole", result)
        return result

    def decide_competitor_registration_tag_candidate(
        self,
        *,
        registration_id: str,
        tag_id: str,
        decision: str,
        edited_tag: str,
        actor: str,
        actor_kind: str,
        reason: str,
    ) -> dict[str, str]:
        if actor_kind != "user" or not actor.strip() or not reason.strip():
            raise StateTransitionError("tag candidate decision requires an explicit user decision and reason")
        if decision not in {"accepted", "rejected"}:
            raise StateTransitionError("tag candidate decision must be accepted or rejected")
        candidates = {item["tag_id"]: item for item in self.list_competitor_registration_tag_candidates(registration_id=registration_id)}
        candidate = candidates.get(tag_id)
        if candidate is None:
            raise StateTransitionError("tag candidate does not belong to this competitor registration")
        if candidate["human_review_status"] != "pending_review":
            raise StateTransitionError("tag candidate has already received a user decision")
        final_tag = edited_tag.strip().lstrip("#") if decision == "accepted" else str(candidate["tag"])
        if decision == "accepted" and not final_tag:
            raise StateTransitionError("an accepted tag candidate requires a non-empty tag")
        registration = self.get_competitor_registration(registration_id=registration_id)
        duplicate = self.conn.execute(
            "SELECT tag_id FROM domain_search_tags WHERE domain_label=? AND tag=? AND tag_id!=? LIMIT 1",
            (registration["domain_label"], final_tag, tag_id),
        ).fetchone()
        if duplicate is not None:
            raise StateTransitionError("the edited tag already exists in this domain")
        review_status = "approved" if decision == "accepted" else "rejected"
        status = "active" if decision == "accepted" else "closed"
        with self.conn:
            self.conn.execute(
                "UPDATE domain_search_tags SET tag=?, status=?, human_review_status=?, reviewed_at=?, reviewed_note=? "
                "WHERE tag_id=? AND domain_label=? AND human_review_status='pending_review'",
                (final_tag, status, review_status, _now(), reason.strip(), tag_id, registration["domain_label"]),
            )
            result = {
                "registration_id": registration_id,
                "tag_id": tag_id,
                "tag": final_tag,
                "status": status,
                "decision": decision,
            }
            self._audit(None, "competitor_registration_tag_candidate_decided", {**result, "actor": actor.strip()})
        return result

    def _persist_competitor_registration_gateway_envelope(
        self, envelope: ModelRunEnvelope, binding: dict[str, Any]
    ) -> str:
        if binding.get("data_identity") != self.data_identity:
            raise DataIdentityError("competitor-registration model result belongs to another data identity")
        registration_id = str(binding.get("registration_id") or "")
        step_name = str(binding.get("step_name") or "")
        if step_name != "breakdown":
            raise ModelGatewayRequiredError(
                "competitor-registration model runs are allowed only for individual hit breakdowns"
            )
        registration = self.get_competitor_registration(registration_id=registration_id)
        prepared_breakdown_steps = {"transcripts_and_comments", "breakdown"}
        active_registration = (
            registration["status"] == "processing"
            and registration["current_step"] in prepared_breakdown_steps
        )
        closed_registration_material = self.conn.execute(
            "SELECT 1 FROM stage0_competitor_registration_item WHERE registration_id=? "
            "AND step_name='transcripts_and_comments' AND item_ref=? AND status='completed' AND data_identity=?",
            (registration_id, str(binding.get("source_id") or ""), self.data_identity),
        ).fetchone() is not None
        if not active_registration and not closed_registration_material:
            raise StaleResultError("competitor-breakdown model result lacks a completed source material")
        model_run_id = _id("competitor_registration_model_run")
        with self.conn:
            self.conn.execute(
                "INSERT INTO stage0_competitor_registration_model_run VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (
                    model_run_id, registration_id, step_name, envelope.status, envelope.route_name,
                    envelope.provider_name, envelope.model_name, envelope.input_hash, envelope.output_hash,
                    _canonical(envelope.error or {}),
                    _canonical({
                        "prompt_tokens": envelope.usage.prompt_tokens,
                        "completion_tokens": envelope.usage.completion_tokens,
                        "total_tokens": envelope.usage.total_tokens,
                    }),
                    _canonical(envelope.cost), envelope.duration_ms, self.data_identity, _now(),
                ),
            )
        return model_run_id

    def _persist_daily_hit_gateway_envelope(
        self, envelope: ModelRunEnvelope, binding: dict[str, Any]
    ) -> str:
        if binding.get("data_identity") != self.data_identity:
            raise DataIdentityError("daily-hit model result belongs to another data identity")
        hit_id = str(binding.get("hit_id") or "")
        hit = self.conn.execute(
            "SELECT preparation_status FROM hits WHERE hit_id=?",
            (hit_id,),
        ).fetchone()
        if hit is None or hit["preparation_status"] != "completed":
            raise StaleResultError("daily-hit model result lacks a completed prepared hit")
        model_run_id = _id("daily_hit_model_run")
        with self.conn:
            self.conn.execute(
                "INSERT INTO stage0_daily_hit_model_run VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (
                    model_run_id,
                    hit_id,
                    envelope.status,
                    envelope.route_name,
                    envelope.provider_name,
                    envelope.model_name,
                    envelope.input_hash,
                    envelope.output_hash,
                    _canonical(envelope.error or {}),
                    _canonical({
                        "prompt_tokens": envelope.usage.prompt_tokens,
                        "completion_tokens": envelope.usage.completion_tokens,
                        "total_tokens": envelope.usage.total_tokens,
                    }),
                    _canonical(envelope.cost),
                    envelope.duration_ms,
                    self.data_identity,
                    _now(),
                ),
            )
        return model_run_id

    def observe_unregistered_account(
        self,
        *,
        domain_label: str,
        account_ref: str,
        account_display_name: str,
        video_ref: str,
        video_url: str,
        video_title: str,
        qualified: bool,
        source_ref: dict[str, Any],
        observed_at: str,
    ) -> dict[str, Any]:
        """Retain an outside-account observation and raise review only at the confirmed three-in-thirty-days boundary."""
        if domain_label not in formal_domain_labels() or not all(
            str(value or "").strip() for value in (account_ref, account_display_name, video_ref, video_title, observed_at)
        ) or not video_url.startswith(("https://", "http://")) or not source_ref:
            raise StateTransitionError("unregistered account observation needs formal identity, accessible video and source evidence")
        try:
            current_time = datetime.fromisoformat(observed_at.replace("Z", "+00:00"))
        except ValueError as exc:
            raise StateTransitionError("unregistered account observation time must be ISO-8601") from exc
        if current_time.tzinfo is None:
            raise StateTransitionError("unregistered account observation time must include a timezone")
        existing = self.conn.execute(
            "SELECT account_video_observation_id FROM stage0_unregistered_account_video "
            "WHERE domain_label=? AND account_ref=? AND video_ref=? AND data_identity=?",
            (domain_label, account_ref.strip(), video_ref.strip(), self.data_identity),
        ).fetchone()
        if existing is None:
            with self.conn:
                self.conn.execute(
                    "INSERT INTO stage0_unregistered_account_video VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                    (_id("unregistered_account_video"), domain_label, account_ref.strip(), account_display_name.strip(),
                     video_ref.strip(), video_url.strip(), video_title.strip(), int(qualified), _canonical(source_ref),
                     observed_at, self.data_identity),
                )
        window_start = (current_time - timedelta(days=30)).isoformat()
        qualified_rows = self.conn.execute(
            "SELECT video_ref, video_url, video_title, observed_at, source_ref_json FROM stage0_unregistered_account_video "
            "WHERE domain_label=? AND account_ref=? AND qualified=1 AND observed_at>=? AND data_identity=? "
            "ORDER BY observed_at DESC, account_video_observation_id DESC",
            (domain_label, account_ref.strip(), window_start, self.data_identity),
        ).fetchall()
        already_open = self.conn.execute(
            "SELECT account_review_id FROM stage0_unregistered_account_review "
            "WHERE domain_label=? AND account_ref=? AND status='awaiting_human_review' AND data_identity=?",
            (domain_label, account_ref.strip(), self.data_identity),
        ).fetchone()
        if len(qualified_rows) < 3 or already_open is not None:
            return {
                "account_ref": account_ref.strip(), "qualified_video_count_30d": len(qualified_rows),
                "account_review_id": already_open["account_review_id"] if already_open else None,
                "human_confirmation_required": already_open is not None,
            }
        trigger_rows = qualified_rows[:3]
        recent_rows = self.conn.execute(
            "SELECT video_ref, video_url, video_title, observed_at FROM stage0_unregistered_account_video "
            "WHERE domain_label=? AND account_ref=? AND data_identity=? ORDER BY observed_at DESC, account_video_observation_id DESC LIMIT 10",
            (domain_label, account_ref.strip(), self.data_identity),
        ).fetchall()
        review_id = _id("unregistered_account_review")
        with self.conn:
            self.conn.execute(
                "INSERT INTO stage0_unregistered_account_review VALUES (?, ?, ?, ?, ?, ?, 'awaiting_human_review', ?, ?, NULL, NULL, NULL)",
                (
                    review_id, domain_label, account_ref.strip(), account_display_name.strip(),
                    _canonical([{"video_ref": row["video_ref"], "video_url": row["video_url"], "video_title": row["video_title"], "observed_at": row["observed_at"], "source_ref": json.loads(row["source_ref_json"])} for row in trigger_rows]),
                    _canonical([{"video_ref": row["video_ref"], "video_url": row["video_url"], "video_title": row["video_title"], "observed_at": row["observed_at"]} for row in recent_rows]),
                    self.data_identity, _now(),
                ),
            )
            self._audit(None, "unregistered_account_ready_for_human_review", {"account_review_id": review_id, "account_ref": account_ref.strip()})
        return {"account_ref": account_ref.strip(), "qualified_video_count_30d": len(qualified_rows), "account_review_id": review_id, "human_confirmation_required": True}

    def decide_unregistered_account_review(
        self,
        *,
        account_review_id: str,
        decision: str,
        owned_account_id: str | None,
        actor: str,
        actor_kind: str,
        reason: str,
        idempotency_key: str,
    ) -> dict[str, Any]:
        if decision not in {"accepted", "rejected"} or actor_kind != "user" or not actor.strip() or not reason.strip():
            raise StateTransitionError("unregistered account review requires an explicit user decision and reason")
        review = self.conn.execute(
            "SELECT * FROM stage0_unregistered_account_review WHERE account_review_id=? AND data_identity=?",
            (account_review_id, self.data_identity),
        ).fetchone()
        if review is None or review["status"] != "awaiting_human_review":
            raise StateTransitionError("unregistered account review is not awaiting a user decision")
        if decision == "rejected":
            with self.conn:
                self.conn.execute(
                    "UPDATE stage0_unregistered_account_review SET status='rejected', decided_by=?, decided_at=?, decision_reason=? WHERE account_review_id=?",
                    (actor.strip(), _now(), reason.strip(), account_review_id),
                )
            return {"account_review_id": account_review_id, "status": "rejected"}
        owned = self.conn.execute(
            "SELECT * FROM stage0_content_account WHERE content_account_id=? AND data_identity=?",
            (owned_account_id, self.data_identity),
        ).fetchone()
        if owned is None or owned["account_role"] != "owned" or owned["domain_label"] != review["domain_label"]:
            raise StateTransitionError("accepting an unregistered account requires an owned account in the same domain")
        competitor_id = _id("competitor_account")
        self.register_content_account(
            content_account_id=competitor_id, account_role="competitor", display_name=review["account_display_name"],
            domain_label=review["domain_label"], external_account_ref=review["account_ref"], actor=actor,
        )
        cold_start = self.start_cold_start(
            cold_start_id=_id("competitor_registration_batch"), owned_account_id=str(owned_account_id),
            competitor_account_ids=(competitor_id,), actor=actor, idempotency_key=idempotency_key,
        )
        with self.conn:
            self.conn.execute(
                "UPDATE stage0_unregistered_account_review SET status='accepted', decided_by=?, decided_at=?, decision_reason=? WHERE account_review_id=?",
                (actor.strip(), _now(), reason.strip(), account_review_id),
            )
        return {
            "account_review_id": account_review_id, "status": "accepted", "competitor_account_id": competitor_id,
            "registration_id": cold_start["registration_ids"][0], "registration_status": "processing",
        }

    def _activate_competitor_daily_tracking(self, *, registration_id: str) -> str:
        registration = self.get_competitor_registration(registration_id=registration_id)
        steps = {item["step_name"]: item["artifact_refs"] for item in self.list_competitor_registration_steps(registration_id=registration_id)}
        if any(step not in steps for step in COMPETITOR_REGISTRATION_STEPS):
            raise StateTransitionError("daily tracking activation requires every competitor registration step")
        account = self.conn.execute(
            "SELECT * FROM stage0_content_account WHERE content_account_id=? AND data_identity=?",
            (registration["competitor_account_id"], self.data_identity),
        ).fetchone()
        if account is None:
            raise StateTransitionError("competitor account disappeared before daily tracking activation")
        history = steps["historical_material"]
        signals = steps["high_signal_identification"]
        if len(history) != 1 or len(signals) != 1:
            raise StateTransitionError("daily tracking activation requires one historical and one signal artifact")
        platform = str(history[0].get("platform") or "").strip()
        external_ref = str(account["external_account_ref"] or "").strip()
        source_value = external_ref[len(platform) + 1:] if external_ref.startswith(f"{platform}:") else external_ref
        if not platform or not source_value:
            raise StateTransitionError("daily tracking activation lacks a platform account identity")
        homepage_url = source_value if source_value.startswith(("https://", "http://")) else (
            f"https://www.douyin.com/user/{source_value}" if platform == "douyin" else source_value
        )
        sec_uid = source_value.rstrip("/").rsplit("/", 1)[-1].split("?", 1)[0]
        existing_account = self.conn.execute(
            "SELECT account_id FROM competitor_accounts WHERE platform=? AND sec_uid=?", (platform, sec_uid)
        ).fetchone()
        tracking_account_id = str(existing_account["account_id"]) if existing_account else str(account["content_account_id"])
        now = datetime.now(timezone.utc)
        selected = {str(item.get("source_id")): item for item in signals[0].get("selected_items", [])}
        materials = {
            item["item_ref"]: item["artifact"] for item in self.list_competitor_registration_items(
                registration_id=registration_id, step_name="transcripts_and_comments"
            ) if item["status"] == "completed"
        }
        breakdowns = {
            item["item_ref"]: item["artifact"] for item in self.list_competitor_registration_items(
                registration_id=registration_id, step_name="breakdown"
            ) if item["status"] == "completed"
        }
        with self.conn:
            if existing_account is None:
                self.conn.execute(
                    "INSERT INTO competitor_accounts("
                    "account_id, platform, domain_label, domain_name, account_name, sec_uid, homepage_url, source_config_ref, "
                    "registration_status, first_crawl_policy, comments_policy"
                    ") VALUES (?, ?, ?, ?, ?, ?, ?, ?, 'active', 'stock_snapshot_archived', 'promoted_hits_only')",
                    (
                        tracking_account_id, platform, account["domain_label"], account["domain_label"], account["display_name"],
                        sec_uid, homepage_url, f"stage0_competitor_registration:{registration_id}",
                    ),
                )
            else:
                self.conn.execute(
                    "UPDATE competitor_accounts SET domain_label=?, domain_name=?, account_name=?, homepage_url=?, "
                    "source_config_ref=?, registration_status='active' WHERE account_id=?",
                    (
                        account["domain_label"], account["domain_label"], account["display_name"], homepage_url,
                        f"stage0_competitor_registration:{registration_id}", tracking_account_id,
                    ),
                )
            for item in history[0].get("items", []):
                source_id = str(item.get("source_id") or "").strip()
                title, url = str(item.get("title") or "").strip(), str(item.get("url") or "").strip()
                if not source_id or not title or not url:
                    continue
                publish_value = item.get("published_at")
                publish_time = None
                if isinstance(publish_value, (int, float)):
                    publish_time = datetime.fromtimestamp(float(publish_value), tz=timezone.utc).isoformat()
                elif str(publish_value or "").strip():
                    raw_publish = str(publish_value).strip()
                    try:
                        parsed_publish = datetime.fromisoformat(raw_publish.replace("Z", "+00:00"))
                        if parsed_publish.tzinfo is None:
                            parsed_publish = parsed_publish.replace(tzinfo=timezone.utc)
                        publish_time = parsed_publish.isoformat()
                    except ValueError:
                        publish_time = None
                category = None
                if publish_time:
                    category = "historical_mature" if datetime.fromisoformat(publish_time) <= now - timedelta(days=7) else "transition"
                metrics = item.get("metrics") if isinstance(item.get("metrics"), dict) else {}
                video_id = "competitor_video_" + _hash({"account_id": tracking_account_id, "source_id": source_id})[:20]
                signal = selected.get(source_id)
                trigger_rules = list(signal.get("signal_channels", [])) if signal else []
                self.conn.execute(
                    "INSERT INTO competitor_videos("
                    "video_id, account_id, platform, platform_item_id, title, url, publish_time, duration_sec, "
                    "like_count, comment_count, share_count, collect_count, first_contact_category, excluded_reason, "
                    "registration_run_id, raw_archive_ref, raw_json, first_trigger_observation, first_trigger_at, "
                    "trigger_rules, peak_observation, baseline_mode, judgment_confidence"
                    ") VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?) "
                    "ON CONFLICT(account_id, platform_item_id) DO UPDATE SET "
                    "title=excluded.title, url=excluded.url, publish_time=excluded.publish_time, duration_sec=excluded.duration_sec, "
                    "like_count=excluded.like_count, comment_count=excluded.comment_count, share_count=excluded.share_count, "
                    "collect_count=excluded.collect_count, registration_run_id=excluded.registration_run_id, "
                    "raw_archive_ref=excluded.raw_archive_ref, raw_json=excluded.raw_json, last_checked_at=CURRENT_TIMESTAMP, "
                    "check_count=competitor_videos.check_count+1",
                    (
                        video_id, tracking_account_id, platform, source_id, title, url, publish_time,
                        int(item.get("duration_seconds") or 0), int(metrics.get("like_count") or 0),
                        int(metrics.get("comment_count") or 0), int(metrics.get("share_count") or 0),
                        int(metrics.get("collect_count") or 0), category, None if category else "missing_publish_time",
                        registration_id, str(history[0].get("raw_archive_ref") or ""), _canonical(item),
                        "registration_history" if signal else None, _now() if signal else None,
                        _canonical(trigger_rules) if signal else None, "registration_history" if signal else None,
                        "mature_history" if signal else None, "rough" if signal else None,
                    ),
                )
                if not signal:
                    continue
                hit_id = "competitor_hit_" + _hash({"account_id": tracking_account_id, "source_id": source_id})[:20]
                self.conn.execute(
                    "INSERT OR IGNORE INTO hits("
                    "hit_id, video_id, account_id, platform, platform_item_id, title, url, publish_time, like_count, "
                    "comment_count, share_count, collect_count, hit_channel, judgment_confidence, baseline_id, run_id, preparation_status"
                    ") VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 'rough', NULL, ?, 'completed')",
                    (
                        hit_id, video_id, tracking_account_id, platform, source_id, title, url, publish_time,
                        int(metrics.get("like_count") or 0), int(metrics.get("comment_count") or 0),
                        int(metrics.get("share_count") or 0), int(metrics.get("collect_count") or 0),
                        ",".join(trigger_rules), registration_id,
                    ),
                )
                material = materials.get(source_id)
                if material:
                    transcript_path = Path(str(material.get("transcript_ref") or ""))
                    if not transcript_path.is_file():
                        raise StateTransitionError("completed competitor material lacks its retained transcript file")
                    transcript_text = transcript_path.read_text(encoding="utf-8").strip()
                    self.conn.execute(
                        "INSERT OR IGNORE INTO hit_transcripts("
                        "transcript_id, hit_id, version, raw_transcript_text, cleaned_transcript_text, char_count, asr_model, "
                        "vad_model, processing_method, audio_sha256, quality_flags, processing_status, run_id"
                        ") VALUES (?, ?, 1, ?, ?, ?, ?, ?, 'local_sensevoice', ?, '', 'completed', ?)",
                        (
                            f"{hit_id}_v1", hit_id, transcript_text, transcript_text, len(transcript_text),
                            str(material.get("asr_model_ref") or "not_reported"),
                            str(material.get("vad_model_ref") or "not_reported"),
                            str(material.get("source_media_hash") or ""), registration_id,
                        ),
                    )
                    for comment in material.get("comments", []):
                        self.conn.execute(
                            "INSERT OR IGNORE INTO hit_comments("
                            "hit_id, comment_id, text, like_count, parent_comment_id, sample_rank, purpose, observation_point, sampling_strategy, run_id"
                            ") VALUES (?, ?, ?, ?, NULL, ?, 'mature_history', NULL, 'top_n_by_platform_popularity', ?)",
                            (
                                hit_id, comment["comment_id"], comment["text"], int(comment["like_count"]),
                                int(comment["sample_rank"]), registration_id,
                            ),
                        )
                breakdown = breakdowns.get(source_id)
                if breakdown:
                    analysis = breakdown.get("analysis") or {}
                    model_run_id = str(breakdown.get("model_run_id") or "configured_business_analysis")
                    model_row = self.conn.execute(
                        "SELECT model_name FROM stage0_competitor_registration_model_run WHERE registration_model_run_id=?",
                        (model_run_id,),
                    ).fetchone()
                    self.conn.execute(
                        "INSERT OR IGNORE INTO hit_deep_analysis("
                        "analysis_id, hit_id, version, request_id, correlation_id, topic_pattern, hook_pattern, "
                        "structure_pattern, model_name, run_id"
                        ") VALUES (?, ?, 1, ?, ?, ?, ?, ?, ?, ?)",
                        (
                            "competitor_analysis_" + _hash({"registration_id": registration_id, "source_id": source_id})[:20],
                            hit_id, model_run_id, f"{registration_id}:{source_id}", str(analysis.get("topic_pattern") or ""),
                            str(analysis.get("hook_pattern") or ""), str(analysis.get("structure_pattern") or ""),
                            str(model_row["model_name"]) if model_row else "configured_business_analysis", registration_id,
                        ),
                    )
        return tracking_account_id

    def complete_competitor_registration(
        self,
        *,
        registration_id: str,
        actor: str,
        idempotency_key: str,
    ) -> dict[str, str]:
        """Complete registration automatically after every required artifact exists."""
        if not actor.strip():
            raise StateTransitionError("competitor registration completion requires an actor")
        registration = self.conn.execute(
            "SELECT * FROM stage0_competitor_registration WHERE registration_id=? AND data_identity=?", (registration_id, self.data_identity)
        ).fetchone()
        if registration is None or registration["status"] != "awaiting_human_review":
            raise StateTransitionError("competitor registration is not ready for automatic completion")
        request = {"registration_id": registration_id, "actor": actor}
        replay = self._replay("complete_competitor_registration", idempotency_key, request)
        if replay:
            # A previous process can have written the completion receipt and
            # daily-tracking material before an interrupted state update became
            # visible.  Replaying must reconcile that proven completed work,
            # never launch collection or model work again.
            tracking_account_id = str(replay.get("daily_tracking_account_id") or "").strip()
            tracking_exists = self.conn.execute(
                "SELECT 1 FROM competitor_accounts WHERE account_id=?",
                (tracking_account_id,),
            ).fetchone()
            if str(replay.get("status") or "") != "completed" or not tracking_account_id or tracking_exists is None:
                raise StateTransitionError(
                    "stored registration completion receipt cannot be reconciled safely"
                )
            now = _now()
            result = {
                "registration_id": registration_id,
                "status": "completed",
                "daily_tracking_account_id": tracking_account_id,
            }
            with self.conn:
                self.conn.execute(
                    "UPDATE stage0_competitor_registration "
                    "SET current_step='completed', status='completed', completed_at=? "
                    "WHERE registration_id=? AND data_identity=? "
                    "AND status='awaiting_human_review'",
                    (now, registration_id, self.data_identity),
                )
                remaining = self.conn.execute(
                    "SELECT 1 FROM stage0_competitor_registration "
                    "WHERE cold_start_id=? AND data_identity=? AND status!='completed' LIMIT 1",
                    (registration["cold_start_id"], self.data_identity),
                ).fetchone()
                if remaining is None:
                    self.conn.execute(
                        "UPDATE stage0_cold_start SET status='awaiting_human_review' "
                        "WHERE cold_start_id=? AND data_identity=? "
                        "AND status='registering_competitors'",
                        (registration["cold_start_id"], self.data_identity),
                    )
                self._audit(
                    None,
                    "competitor_registration_completion_reconciled_from_receipt",
                    result,
                )
            return result
        tracking_account_id = self._activate_competitor_daily_tracking(registration_id=registration_id)
        now = _now()
        with self.conn:
            self.conn.execute(
                "UPDATE stage0_competitor_registration SET current_step='completed', status='completed', completed_at=? WHERE registration_id=?",
                (now, registration_id),
            )
            remaining = self.conn.execute(
                "SELECT 1 FROM stage0_competitor_registration WHERE cold_start_id=? AND data_identity=? AND status!='completed' LIMIT 1",
                (registration["cold_start_id"], self.data_identity),
            ).fetchone()
            if remaining is None:
                self.conn.execute(
                    "UPDATE stage0_cold_start SET status='awaiting_human_review' WHERE cold_start_id=? AND data_identity=? AND status='registering_competitors'",
                    (registration["cold_start_id"], self.data_identity),
                )
            result = {"registration_id": registration_id, "status": "completed", "daily_tracking_account_id": tracking_account_id}
            self._receipt("complete_competitor_registration", idempotency_key, request, result)
            self._audit(None, "competitor_registration_completed_automatically", result)
        return result

    def confirm_cold_start(
        self,
        *,
        cold_start_id: str,
        actor: str,
        actor_kind: str,
        reason: str,
        idempotency_key: str,
    ) -> dict[str, str]:
        """Accept a cold start only after every required competitor registration is complete."""
        if actor_kind != "user" or not actor.strip() or not reason.strip():
            raise StateTransitionError("cold start confirmation requires an explicit user decision")
        cold_start = self.conn.execute(
            "SELECT * FROM stage0_cold_start WHERE cold_start_id=? AND data_identity=?", (cold_start_id, self.data_identity)
        ).fetchone()
        if cold_start is None or cold_start["status"] != "awaiting_human_review":
            raise StateTransitionError("cold start is not awaiting human review")
        missing_breakdowns = self.conn.execute(
            "SELECT COUNT(*) FROM stage0_competitor_registration_item material "
            "JOIN stage0_competitor_registration registration "
            "ON registration.registration_id=material.registration_id "
            "LEFT JOIN stage0_competitor_registration_item breakdown "
            "ON breakdown.registration_id=material.registration_id AND breakdown.item_ref=material.item_ref "
            "AND breakdown.data_identity=material.data_identity AND breakdown.step_name='breakdown' "
            "WHERE registration.cold_start_id=? AND material.data_identity=? "
            "AND material.step_name='transcripts_and_comments' AND material.status='completed' "
            "AND (breakdown.status IS NULL OR breakdown.status!='completed')",
            (cold_start_id, self.data_identity),
        ).fetchone()[0]
        if int(missing_breakdowns):
            raise StateTransitionError(
                f"cold start still needs {int(missing_breakdowns)} completed deep breakdowns before final confirmation"
            )
        request = {"cold_start_id": cold_start_id, "actor": actor, "reason": reason}
        replay = self._replay("confirm_cold_start", idempotency_key, request)
        if replay:
            return replay
        now = _now()
        with self.conn:
            self.conn.execute(
                "UPDATE stage0_cold_start SET status='completed', completed_at=? WHERE cold_start_id=?",
                (now, cold_start_id),
            )
            self.conn.execute(
                "UPDATE stage0_cold_start_configuration SET status='completed' "
                "WHERE cold_start_id=? AND data_identity=? AND status='started'",
                (cold_start_id, self.data_identity),
            )
            result = {"cold_start_id": cold_start_id, "status": "completed"}
            self._receipt("confirm_cold_start", idempotency_key, request, result)
            self._audit(None, "cold_start_confirmed", result)
        return result

    def discard_cold_start_breakdown_history(
        self,
        *,
        cold_start_id: str,
        actor: str,
        actor_kind: str,
        reason: str,
        idempotency_key: str,
    ) -> dict[str, int | str]:
        """Permanently remove every obsolete breakdown result before one full replacement run.

        Original collected material is intentionally retained: the replacement
        Skill must use the same formal source, transcript, and comments rather
        than silently recollecting or falling back to a retired analysis.
        """
        if actor_kind != "user" or not actor.strip() or not reason.strip():
            raise StateTransitionError("breakdown replacement requires an explicit user decision")
        cold_start = self.conn.execute(
            "SELECT status FROM stage0_cold_start WHERE cold_start_id=? AND data_identity=?",
            (cold_start_id, self.data_identity),
        ).fetchone()
        if cold_start is None or str(cold_start["status"]) != "awaiting_human_review":
            raise StateTransitionError("breakdown replacement requires a cold start awaiting final review")
        request = {"cold_start_id": cold_start_id, "actor": actor, "reason": reason}
        replay = self._replay("discard_cold_start_breakdown_history", idempotency_key, request)
        if replay:
            return replay
        registrations = self.conn.execute(
            "SELECT registration_id FROM stage0_competitor_registration "
            "WHERE cold_start_id=? AND data_identity=?",
            (cold_start_id, self.data_identity),
        ).fetchall()
        registration_ids = [str(row["registration_id"]) for row in registrations]
        if not registration_ids:
            raise StateTransitionError("breakdown replacement requires registered competitor accounts")
        placeholders = ",".join("?" for _ in registration_ids)
        parameters = tuple(registration_ids) + (self.data_identity,)
        material_count = int(self.conn.execute(
            "SELECT COUNT(*) FROM stage0_competitor_registration_item "
            f"WHERE registration_id IN ({placeholders}) AND data_identity=? "
            "AND step_name='transcripts_and_comments' AND status='completed'",
            parameters,
        ).fetchone()[0])
        if not material_count:
            raise StateTransitionError("breakdown replacement requires retained completed source materials")
        with self.conn:
            breakdown_count = int(self.conn.execute(
                "SELECT COUNT(*) FROM stage0_competitor_registration_item "
                f"WHERE registration_id IN ({placeholders}) AND data_identity=? AND step_name='breakdown'",
                parameters,
            ).fetchone()[0])
            model_run_count = int(self.conn.execute(
                "SELECT COUNT(*) FROM stage0_competitor_registration_model_run "
                f"WHERE registration_id IN ({placeholders}) AND data_identity=? AND step_name='breakdown'",
                parameters,
            ).fetchone()[0])
            self.conn.execute(
                "DELETE FROM stage0_competitor_registration_model_run "
                f"WHERE registration_id IN ({placeholders}) AND data_identity=? AND step_name='breakdown'",
                parameters,
            )
            # A replacement run is the one explicit exception to the normal
            # immutable-step rule: the user has retired this entire prior
            # breakdown route and requested physical removal, not a fallback.
            self.conn.execute(
                "DROP TRIGGER IF EXISTS stage0_competitor_registration_step_immutable_delete"
            )
            self.conn.execute(
                "DELETE FROM stage0_competitor_registration_step "
                f"WHERE registration_id IN ({placeholders}) AND data_identity=? AND step_name='breakdown'",
                parameters,
            )
            self.conn.execute(
                "CREATE TRIGGER stage0_competitor_registration_step_immutable_delete "
                "BEFORE DELETE ON stage0_competitor_registration_step "
                "BEGIN SELECT RAISE(ABORT, 'competitor registration steps are immutable'); END"
            )
            self.conn.execute(
                "DELETE FROM stage0_competitor_registration_item "
                f"WHERE registration_id IN ({placeholders}) AND data_identity=? AND step_name='breakdown'",
                parameters,
            )
            result: dict[str, int | str] = {
                "cold_start_id": cold_start_id,
                "status": "breakdown_history_discarded",
                "retained_source_materials": material_count,
                "discarded_breakdown_records": breakdown_count,
                "discarded_breakdown_model_runs": model_run_count,
            }
            self._receipt("discard_cold_start_breakdown_history", idempotency_key, request, result)
            self._audit(None, "cold_start_breakdown_history_discarded", result)
        return result

    def list_cold_start_breakdown_materials(self, *, cold_start_id: str) -> list[dict[str, Any]]:
        """Return retained formal source materials for the one current replacement run."""
        rows = self.conn.execute(
            "SELECT item.registration_id, item.item_ref, item.artifact_json "
            "FROM stage0_competitor_registration_item item "
            "JOIN stage0_competitor_registration registration ON registration.registration_id=item.registration_id "
            "WHERE registration.cold_start_id=? AND item.data_identity=? "
            "AND item.step_name='transcripts_and_comments' AND item.status='completed' "
            "ORDER BY item.registration_id, item.item_ref",
            (cold_start_id, self.data_identity),
        ).fetchall()
        return [
            {
                "registration_id": str(row["registration_id"]),
                "source_id": str(row["item_ref"]),
                "material": _hide_build_root_paths(json.loads(str(row["artifact_json"]))),
            }
            for row in rows
        ]

    def create_competitor_breakdown_backlog_task(
        self,
        *,
        cold_start_id: str,
        approval: dict[str, str],
        expected_pending_count: int,
    ) -> dict[str, Any]:
        """Freeze one explicit formal backlog before any model call begins."""
        if expected_pending_count < 1:
            raise StateTransitionError("formal breakdown backlog needs a positive expected material count")
        if not str(approval.get("actor") or "").strip() or not str(approval.get("conversation_ref") or "").strip():
            raise StateTransitionError("formal breakdown backlog needs recorded user approval")
        cold_start = self.conn.execute(
            "SELECT 1 FROM stage0_cold_start WHERE cold_start_id=? AND data_identity=?",
            (cold_start_id, self.data_identity),
        ).fetchone()
        if cold_start is None:
            raise StateTransitionError("formal breakdown backlog does not belong to this data identity")
        active = self.conn.execute(
            "SELECT backlog_task_id FROM stage0_competitor_breakdown_backlog_task "
            "WHERE cold_start_id=? AND data_identity=? AND status IN ('queued', 'running') LIMIT 1",
            (cold_start_id, self.data_identity),
        ).fetchone()
        if active is not None:
            raise StateTransitionError("this cold-start already has an active formal breakdown backlog task")
        materials = self.list_cold_start_breakdown_materials(cold_start_id=cold_start_id)
        existing_rows = self.conn.execute(
            "SELECT item.registration_id, item.item_ref, item.status "
            "FROM stage0_competitor_registration_item item "
            "JOIN stage0_competitor_registration registration ON registration.registration_id=item.registration_id "
            "WHERE registration.cold_start_id=? AND item.data_identity=? AND item.step_name='breakdown'",
            (cold_start_id, self.data_identity),
        ).fetchall()
        existing = {(str(row["registration_id"]), str(row["item_ref"])): str(row["status"]) for row in existing_rows}
        snapshot = [
            {"registration_id": str(item["registration_id"]), "source_id": str(item["source_id"])}
            for item in materials
            if (str(item["registration_id"]), str(item["source_id"])) not in existing
        ]
        if len(snapshot) != expected_pending_count:
            raise StateTransitionError(
                f"formal breakdown backlog changed before launch: expected {expected_pending_count}, found {len(snapshot)}"
            )
        now = _now()
        task_id = _id("competitor_breakdown_backlog")
        summary = {"source_count": len(snapshot), "completed": 0, "failed": 0, "pending": len(snapshot)}
        with self.conn:
            self.conn.execute(
                "INSERT INTO stage0_competitor_breakdown_backlog_task("
                "backlog_task_id, cold_start_id, status, source_snapshot_json, approval_json, summary_json, "
                "data_identity, created_at, updated_at, completed_at) VALUES (?, ?, 'queued', ?, ?, ?, ?, ?, ?, NULL)",
                (task_id, cold_start_id, _canonical(snapshot), _canonical(approval), _canonical(summary), self.data_identity, now, now),
            )
            result = self.get_competitor_breakdown_backlog_task(backlog_task_id=task_id)
            self._audit(None, "competitor_breakdown_backlog_task_created", result)
        return result

    def get_competitor_breakdown_backlog_task(self, *, backlog_task_id: str) -> dict[str, Any]:
        row = self.conn.execute(
            "SELECT * FROM stage0_competitor_breakdown_backlog_task WHERE backlog_task_id=? AND data_identity=?",
            (backlog_task_id, self.data_identity),
        ).fetchone()
        if row is None:
            raise StateTransitionError("formal breakdown backlog task does not exist in this data identity")
        return {
            "backlog_task_id": str(row["backlog_task_id"]),
            "cold_start_id": str(row["cold_start_id"]),
            "status": str(row["status"]),
            "source_snapshot": json.loads(str(row["source_snapshot_json"])),
            "approval": json.loads(str(row["approval_json"])),
            "summary": json.loads(str(row["summary_json"])),
            "created_at": str(row["created_at"]),
            "updated_at": str(row["updated_at"]),
            "completed_at": row["completed_at"],
        }

    def competitor_breakdown_backlog_task_progress(self, *, backlog_task_id: str) -> dict[str, Any]:
        task = self.get_competitor_breakdown_backlog_task(backlog_task_id=backlog_task_id)
        snapshot = task["source_snapshot"]
        rows = self.conn.execute(
            "SELECT item.registration_id, item.item_ref, item.status "
            "FROM stage0_competitor_registration_item item "
            "WHERE item.data_identity=? AND item.step_name='breakdown'",
            (self.data_identity,),
        ).fetchall()
        states = {(str(row["registration_id"]), str(row["item_ref"])): str(row["status"]) for row in rows}
        completed = failed = 0
        for item in snapshot:
            status = states.get((str(item["registration_id"]), str(item["source_id"])))
            if status == "completed":
                completed += 1
            elif status == "failed":
                failed += 1
        return {
            **task,
            "summary": {
                "source_count": len(snapshot),
                "completed": completed,
                "failed": failed,
                "pending": len(snapshot) - completed - failed,
            },
        }

    def update_competitor_breakdown_backlog_task(
        self,
        *,
        backlog_task_id: str,
        status: str,
        summary: dict[str, Any],
    ) -> dict[str, Any]:
        if status not in {"queued", "running", "completed", "completed_with_failures"}:
            raise StateTransitionError("formal breakdown backlog task has an unsupported status")
        self.get_competitor_breakdown_backlog_task(backlog_task_id=backlog_task_id)
        now = _now()
        completed_at = now if status in {"completed", "completed_with_failures"} else None
        with self.conn:
            self.conn.execute(
                "UPDATE stage0_competitor_breakdown_backlog_task SET status=?, summary_json=?, updated_at=?, completed_at=? "
                "WHERE backlog_task_id=? AND data_identity=?",
                (status, _canonical(summary), now, completed_at, backlog_task_id, self.data_identity),
            )
        return self.get_competitor_breakdown_backlog_task(backlog_task_id=backlog_task_id)

    def discard_failed_breakdown_for_replacement(
        self,
        *,
        cold_start_id: str,
        registration_id: str,
        source_id: str,
        actor: str,
        actor_kind: str,
        reason: str,
        idempotency_key: str,
    ) -> dict[str, int | str]:
        """Remove one current invalid failed result before its explicitly authorized retry."""
        if actor_kind != "user" or not actor.strip() or not reason.strip():
            raise StateTransitionError("breakdown retry requires an explicit user decision")
        request = {
            "cold_start_id": cold_start_id,
            "registration_id": registration_id,
            "source_id": source_id,
            "actor": actor,
            "reason": reason,
        }
        replay = self._replay("discard_failed_breakdown_for_replacement", idempotency_key, request)
        if replay:
            return replay
        material = self.conn.execute(
            "SELECT 1 FROM stage0_competitor_registration_item item "
            "JOIN stage0_competitor_registration registration ON registration.registration_id=item.registration_id "
            "WHERE registration.cold_start_id=? AND item.registration_id=? AND item.item_ref=? "
            "AND item.data_identity=? AND item.step_name='transcripts_and_comments' AND item.status='completed'",
            (cold_start_id, registration_id, source_id, self.data_identity),
        ).fetchone()
        failed = self.conn.execute(
            "SELECT status FROM stage0_competitor_registration_item WHERE registration_id=? AND item_ref=? "
            "AND data_identity=? AND step_name='breakdown'",
            (registration_id, source_id, self.data_identity),
        ).fetchone()
        if material is None or failed is None or str(failed["status"]) != "failed":
            raise StateTransitionError("only one current failed breakdown with retained source material may be retried")
        with self.conn:
            model_run_count = int(self.conn.execute(
                "SELECT COUNT(*) FROM stage0_competitor_registration_model_run "
                "WHERE registration_id=? AND data_identity=? AND step_name='breakdown'",
                (registration_id, self.data_identity),
            ).fetchone()[0])
            self.conn.execute(
                "DELETE FROM stage0_competitor_registration_model_run "
                "WHERE registration_id=? AND data_identity=? AND step_name='breakdown'",
                (registration_id, self.data_identity),
            )
            self.conn.execute(
                "DELETE FROM stage0_competitor_registration_item WHERE registration_id=? AND item_ref=? "
                "AND data_identity=? AND step_name='breakdown' AND status='failed'",
                (registration_id, source_id, self.data_identity),
            )
            result: dict[str, int | str] = {
                "cold_start_id": cold_start_id,
                "registration_id": registration_id,
                "source_id": source_id,
                "status": "failed_breakdown_discarded_for_replacement",
                "discarded_breakdown_records": 1,
                "discarded_breakdown_model_runs": model_run_count,
            }
            self._receipt("discard_failed_breakdown_for_replacement", idempotency_key, request, result)
            self._audit(None, "failed_breakdown_discarded_for_replacement", result)
        return result

    def record_knowledge_mirror_run(
        self,
        *,
        cold_start_id: str | None,
        mirror_root: str,
        record_count: int,
        relationship_count: int,
        actor: str,
    ) -> dict[str, Any]:
        if not mirror_root.strip() or record_count < 0 or relationship_count < 0 or not actor.strip():
            raise StateTransitionError("knowledge mirror completion requires a concrete result and actor")
        if cold_start_id:
            row = self.conn.execute(
                "SELECT 1 FROM stage0_cold_start WHERE cold_start_id=? AND data_identity=?",
                (cold_start_id, self.data_identity),
            ).fetchone()
            if row is None:
                raise StateTransitionError("knowledge mirror cold start does not exist")
        run_id = _id("knowledge_mirror_run")
        with self.conn:
            self.conn.execute(
                "INSERT INTO stage0_knowledge_mirror_run VALUES (?, ?, ?, ?, ?, 'completed', ?, ?, ?)",
                (
                    run_id,
                    cold_start_id,
                    mirror_root,
                    record_count,
                    relationship_count,
                    self.data_identity,
                    actor.strip(),
                    _now(),
                ),
            )
        return {
            "mirror_run_id": run_id,
            "cold_start_id": cold_start_id,
            "status": "completed",
            "record_count": record_count,
            "relationship_count": relationship_count,
        }

    def link_manual_source(self, *, manual_source_id: str, target_kind: str, target_id: str) -> dict[str, str]:
        """Keep the same manual origin attached when its handling path changes."""
        if target_kind not in MANUAL_SOURCE_TARGET_KINDS:
            raise StateTransitionError("manual source target kind is not supported")
        if not target_id.strip():
            raise StateTransitionError("manual source link requires a target")
        source = self.conn.execute(
            "SELECT data_identity FROM stage0_manual_source WHERE manual_source_id=?", (manual_source_id,)
        ).fetchone()
        if source is None or source["data_identity"] != self.data_identity:
            raise StateTransitionError("manual source does not exist in this data identity")
        link_id = _id("manual_source_link")
        with self.conn:
            self.conn.execute(
                """
                INSERT INTO stage0_manual_source_link(
                    link_id, manual_source_id, target_kind, target_id, data_identity, linked_at
                ) VALUES (?, ?, ?, ?, ?, ?)
                """,
                (link_id, manual_source_id, target_kind, target_id.strip(), self.data_identity, _now()),
            )
        return {"link_id": link_id, "manual_source_id": manual_source_id, "target_kind": target_kind, "target_id": target_id.strip()}

    def route_manual_source_to_exploration(
        self,
        *,
        manual_source_id: str,
        actor: str,
        actor_kind: str,
        scope: dict[str, Any],
    ) -> dict[str, str]:
        """Route a user-provided person, one work, or a work set without turning it into a topic by itself."""
        if actor_kind != "user" or not actor.strip() or not scope:
            raise StateTransitionError("manual exploration requires a user, a retained source and an explicit scope")
        source = self.conn.execute(
            "SELECT source_kind, data_identity FROM stage0_manual_source WHERE manual_source_id=?",
            (manual_source_id,),
        ).fetchone()
        if source is None or source["data_identity"] != self.data_identity:
            raise StateTransitionError("manual source does not exist in this data identity")
        exploration_kind_by_source = {
            "person": "person_exploration",
            "work": "work_exploration",
            "playlist": "playlist_exploration",
        }
        exploration_kind = exploration_kind_by_source.get(source["source_kind"])
        if exploration_kind is None:
            raise StateTransitionError("only a person, one work, or a work set may enter an exploration route")
        exploration_id = _id("manual_exploration")
        now = _now()
        with self.conn:
            self.conn.execute(
                "INSERT INTO stage0_manual_exploration VALUES (?, ?, ?, ?, 'awaiting_material_collection', ?, ?, ?)",
                (exploration_id, manual_source_id, exploration_kind, _canonical(scope), self.data_identity, actor.strip(), now),
            )
            self.conn.execute(
                "INSERT INTO stage0_manual_exploration_state VALUES (?, ?, 'awaiting_material_collection', ?, ?, ?)",
                (_id("manual_exploration_state"), exploration_id, "user source retained and routed before any formal topic is created", self.data_identity, now),
            )
            self.conn.execute(
                "INSERT INTO stage0_manual_source_link VALUES (?, ?, ?, ?, ?, ?)",
                (_id("manual_source_link"), manual_source_id, exploration_kind, exploration_id, self.data_identity, now),
            )
            self._audit(None, "manual_source_routed_to_exploration", {"manual_source_id": manual_source_id, "exploration_id": exploration_id, "exploration_kind": exploration_kind})
        return {"manual_source_id": manual_source_id, "exploration_id": exploration_id, "exploration_kind": exploration_kind, "status": "awaiting_material_collection"}

    def begin_manual_exploration_collection(self, *, exploration_id: str, actor: str, reason: str) -> dict[str, str]:
        """Open the one selected person/work/playlist exploration for controlled material collection."""
        exploration = self.conn.execute(
            "SELECT * FROM stage0_manual_exploration WHERE exploration_id=? AND data_identity=?",
            (exploration_id, self.data_identity),
        ).fetchone()
        if exploration is None or not actor.strip() or not reason.strip():
            raise StateTransitionError("manual exploration collection requires a retained exploration, actor and reason")
        current = self.conn.execute(
            "SELECT status FROM stage0_manual_exploration_state WHERE exploration_id=? AND data_identity=? "
            "ORDER BY effective_at DESC, exploration_state_id DESC LIMIT 1",
            (exploration_id, self.data_identity),
        ).fetchone()
        if current is None or current["status"] != "awaiting_material_collection":
            raise StateTransitionError("manual exploration is not awaiting collection")
        with self.conn:
            self.conn.execute(
                "INSERT INTO stage0_manual_exploration_state VALUES (?, ?, 'collecting', ?, ?, ?)",
                (_id("manual_exploration_state"), exploration_id, reason.strip(), self.data_identity, _now()),
            )
        return {"exploration_id": exploration_id, "exploration_kind": str(exploration["exploration_kind"]), "status": "collecting"}

    def record_manual_exploration_material(
        self,
        *,
        exploration_id: str,
        material_ref: dict[str, Any],
        evidence_role: str,
        collected_at: str,
    ) -> dict[str, str]:
        """Retain one external material reference without replacing the user's origin source."""
        if evidence_role not in {"fact_evidence", "professional_interpretation", "audience_perception", "video_material", "research_clue"}:
            raise StateTransitionError("manual exploration material has an unsupported evidence role")
        if not material_ref or not str(material_ref.get("source_ref") or material_ref.get("url") or "").strip() or not collected_at.strip():
            raise StateTransitionError("manual exploration material needs a traceable collected source reference and time")
        current = self.conn.execute(
            "SELECT status FROM stage0_manual_exploration_state WHERE exploration_id=? AND data_identity=? "
            "ORDER BY effective_at DESC, exploration_state_id DESC LIMIT 1",
            (exploration_id, self.data_identity),
        ).fetchone()
        if current is None or current["status"] != "collecting":
            raise StateTransitionError("manual exploration materials may only be retained while collecting")
        material_id = _id("manual_exploration_material")
        with self.conn:
            self.conn.execute(
                "INSERT INTO stage0_manual_exploration_material VALUES (?, ?, ?, ?, ?, ?)",
                (material_id, exploration_id, _canonical(material_ref), evidence_role, self.data_identity, collected_at),
            )
        return {"exploration_material_id": material_id, "exploration_id": exploration_id, "evidence_role": evidence_role}

    def record_music_audience_echo(
        self, *, exploration_id: str, materials: tuple[dict[str, Any], ...], collected_at: str
    ) -> dict[str, Any]:
        exploration = self.conn.execute(
            "SELECT exploration_kind FROM stage0_manual_exploration WHERE exploration_id=? AND data_identity=?",
            (exploration_id, self.data_identity),
        ).fetchone()
        current = self.conn.execute(
            "SELECT status FROM stage0_manual_exploration_state WHERE exploration_id=? AND data_identity=? "
            "ORDER BY effective_at DESC, exploration_state_id DESC LIMIT 1",
            (exploration_id, self.data_identity),
        ).fetchone()
        if exploration is None or exploration["exploration_kind"] != "person_exploration" or current is None or current["status"] != "collecting":
            raise StateTransitionError("music audience materials require an active person exploration")
        retained = 0
        with self.conn:
            for material in materials:
                platform = str(material.get("platform") or "")
                subject_kind = str(material.get("subject_kind") or "")
                material_kind = str(material.get("material_kind") or "")
                text = str(material.get("text") or "").strip()
                useful = int(material.get("useful_count") or 0)
                source_url = str(material.get("source_url") or "").strip()
                if (
                    platform not in {"netease_music", "douban"}
                    or subject_kind not in {"song", "album"}
                    or material_kind not in {"comment", "short_review", "long_review"}
                    or useful < 1 or len(text) < 5 or not source_url.startswith(("http://", "https://"))
                ):
                    raise StateTransitionError("music audience material violates its retained-source boundary")
                echo_id = _id("music_audience_echo")
                cursor = self.conn.execute(
                    "INSERT OR IGNORE INTO stage0_music_audience_echo VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                    (
                        echo_id, exploration_id, platform, str(material.get("work_title") or "").strip(),
                        subject_kind, material_kind, text, useful, source_url,
                        int(material.get("viewed_position") or 0), self.data_identity, collected_at,
                    ),
                )
                if cursor.rowcount:
                    retained += 1
                    material_ref = {
                        "source_ref": f"music_audience_echo:{echo_id}", "url": source_url,
                        "platform": platform, "work_title": str(material.get("work_title") or "").strip(),
                        "subject_kind": subject_kind, "material_kind": material_kind,
                    }
                    self.conn.execute(
                        "INSERT INTO stage0_manual_exploration_material VALUES (?, ?, ?, 'audience_perception', ?, ?)",
                        (_id("manual_exploration_material"), exploration_id, _canonical(material_ref), self.data_identity, collected_at),
                    )
        return {"exploration_id": exploration_id, "retained_count": retained, "evidence_role": "audience_perception"}

    def complete_manual_exploration_collection(self, *, exploration_id: str, actor: str, reason: str) -> dict[str, str]:
        """Finish collection and stop for a user topic or direction decision; it never creates a formal topic."""
        if not actor.strip() or not reason.strip():
            raise StateTransitionError("manual exploration completion requires an actor and reason")
        current = self.conn.execute(
            "SELECT status FROM stage0_manual_exploration_state WHERE exploration_id=? AND data_identity=? "
            "ORDER BY effective_at DESC, exploration_state_id DESC LIMIT 1",
            (exploration_id, self.data_identity),
        ).fetchone()
        material_count = self.conn.execute(
            "SELECT COUNT(*) FROM stage0_manual_exploration_material WHERE exploration_id=? AND data_identity=?",
            (exploration_id, self.data_identity),
        ).fetchone()[0]
        if current is None or current["status"] != "collecting" or int(material_count) < 1:
            raise StateTransitionError("manual exploration needs collected material before it can await a user direction")
        with self.conn:
            self.conn.execute(
                "INSERT INTO stage0_manual_exploration_state VALUES (?, ?, 'awaiting_human_direction', ?, ?, ?)",
                (_id("manual_exploration_state"), exploration_id, reason.strip(), self.data_identity, _now()),
            )
        return {"exploration_id": exploration_id, "status": "awaiting_human_direction", "material_count": str(material_count)}

    def complete_manual_exploration_direction(
        self,
        *,
        exploration_id: str,
        formal_topic_id: str,
        actor: str,
        reason: str,
    ) -> dict[str, str]:
        """Close an exploration only after the user-selected direction became a formal topic."""
        if not formal_topic_id.strip() or not actor.strip() or not reason.strip():
            raise StateTransitionError(
                "manual exploration completion requires the formal topic, user and reason"
            )
        exploration = self.conn.execute(
            "SELECT manual_source_id FROM stage0_manual_exploration "
            "WHERE exploration_id=? AND data_identity=?",
            (exploration_id, self.data_identity),
        ).fetchone()
        current = self.conn.execute(
            "SELECT status FROM stage0_manual_exploration_state "
            "WHERE exploration_id=? AND data_identity=? "
            "ORDER BY effective_at DESC, exploration_state_id DESC LIMIT 1",
            (exploration_id, self.data_identity),
        ).fetchone()
        linked = (
            self.conn.execute(
                "SELECT 1 FROM stage0_manual_source_link "
                "WHERE manual_source_id=? AND target_kind='formal_topic' "
                "AND target_id=? AND data_identity=?",
                (
                    exploration["manual_source_id"],
                    formal_topic_id.strip(),
                    self.data_identity,
                ),
            ).fetchone()
            if exploration is not None
            else None
        )
        if (
            exploration is None
            or current is None
            or current["status"] != "awaiting_human_direction"
            or linked is None
        ):
            raise StateTransitionError(
                "manual exploration may complete only after its retained source is linked to the user-confirmed formal topic"
            )
        now = _now()
        with self.conn:
            self.conn.execute(
                "INSERT INTO stage0_manual_exploration_state VALUES (?, ?, 'completed', ?, ?, ?)",
                (
                    _id("manual_exploration_state"),
                    exploration_id,
                    reason.strip(),
                    self.data_identity,
                    now,
                ),
            )
        return {
            "exploration_id": exploration_id,
            "formal_topic_id": formal_topic_id.strip(),
            "status": "completed",
        }

    def get_manual_exploration_packet(self, *, exploration_id: str) -> dict[str, Any]:
        """Return the retained manual origin and collected materials for the required human direction decision."""
        exploration = self.conn.execute(
            "SELECT exploration.*, source.original_content_json, source.source_kind, "
            "source.domain_label, source.account_ref, source.submitted_by "
            "FROM stage0_manual_exploration exploration "
            "JOIN stage0_manual_source source ON source.manual_source_id=exploration.manual_source_id "
            "WHERE exploration.exploration_id=? AND exploration.data_identity=? AND source.data_identity=?",
            (exploration_id, self.data_identity, self.data_identity),
        ).fetchone()
        if exploration is None:
            raise StateTransitionError("manual exploration does not exist in this data identity")
        state = self.conn.execute(
            "SELECT status, reason, effective_at FROM stage0_manual_exploration_state WHERE exploration_id=? AND data_identity=? "
            "ORDER BY effective_at DESC, exploration_state_id DESC LIMIT 1",
            (exploration_id, self.data_identity),
        ).fetchone()
        materials = self.conn.execute(
            "SELECT material_ref_json, evidence_role, collected_at FROM stage0_manual_exploration_material "
            "WHERE exploration_id=? AND data_identity=? ORDER BY collected_at, exploration_material_id",
            (exploration_id, self.data_identity),
        ).fetchall()
        return {
            "exploration_id": exploration_id,
            "manual_source_id": exploration["manual_source_id"],
            "exploration_kind": exploration["exploration_kind"],
            "scope": json.loads(exploration["scope_json"]),
            "manual_origin": {"source_kind": exploration["source_kind"], "original_content": json.loads(exploration["original_content_json"])},
            "domain_label": exploration["domain_label"],
            "account_ref": exploration["account_ref"],
            "submitted_by": exploration["submitted_by"],
            "status": state["status"],
            "status_reason": state["reason"],
            "materials": [
                {"material": json.loads(row["material_ref_json"]), "evidence_role": row["evidence_role"], "collected_at": row["collected_at"]}
                for row in materials
            ],
            "user_direction_required": state["status"] == "awaiting_human_direction",
        }

    def create_direct_formal_topic(
        self,
        *,
        domain_label: str,
        account_ref: str,
        core_question: str,
        scope_or_requirement: str,
        original_instruction: Any,
        actor: str,
        actor_kind: str,
        existing_manual_source_ids: tuple[str, ...] = (),
        known_materials: list[dict[str, Any]] | None = None,
        material_gaps: list[str] | None = None,
        timeliness: dict[str, Any] | None = None,
        risks: list[str] | None = None,
        related_topic_refs: list[dict[str, Any]] | None = None,
        idempotency_key: str,
    ) -> dict[str, str]:
        """Record an explicit user-issued formal topic: it bypasses candidates and is already topic-confirmed."""
        domain_label = self._require_configured_domain(domain_label, context="direct formal topic")
        if actor_kind != "user":
            raise StateTransitionError("direct formal topic requires a user and a configured domain")
        if not account_ref.strip() or len(core_question.strip()) < 6 or not scope_or_requirement.strip() or original_instruction is None:
            raise StateTransitionError("direct formal topic requires account, core question, scope and the original user instruction")
        self._require_owned_account_for_domain(domain_label=domain_label, account_ref=account_ref.strip())
        request = {
            "domain_label": domain_label, "account_ref": account_ref.strip(), "core_question": core_question.strip(),
            "scope_or_requirement": scope_or_requirement.strip(), "original_instruction": original_instruction,
            "existing_manual_source_ids": list(existing_manual_source_ids), "known_materials": known_materials or [],
            "material_gaps": material_gaps or [], "timeliness": timeliness or {}, "risks": risks or [],
            "related_topic_refs": related_topic_refs or [], "actor": actor,
        }
        replay = self._replay("create_direct_formal_topic", idempotency_key, request)
        if replay:
            return replay
        for manual_source_id in existing_manual_source_ids:
            source = self.conn.execute(
                "SELECT data_identity, domain_label FROM stage0_manual_source WHERE manual_source_id=?",
                (manual_source_id,),
            ).fetchone()
            if source is None or source["data_identity"] != self.data_identity:
                raise StateTransitionError("an attached manual source does not exist in this data identity")
            if source["domain_label"] != domain_label:
                raise StateTransitionError("an attached manual source belongs to another domain")
        task_id, topic_version_id, manual_source_id = _id("task"), _id("version"), _id("manual_direction")
        now = _now()
        instruction_source = {
            "instruction": original_instruction,
            "core_question": core_question.strip(),
            "scope_or_requirement": scope_or_requirement.strip(),
        }
        topic_payload = {
            "title": core_question.strip(), "core_question": core_question.strip(), "domain": domain_label,
            "account_ref": account_ref.strip(), "scope_or_requirement": scope_or_requirement.strip(),
            "topic_origin": "direct_user_instruction", "known_materials": known_materials or [],
            "material_gaps": material_gaps or [], "timeliness": timeliness or {}, "risks": risks or [],
            "related_topic_refs": related_topic_refs or [],
            "source_refs": [{"kind": "manual_source", "manual_source_id": manual_source_id}],
        }
        with self.conn:
            self.conn.execute(
                "INSERT INTO stage0_manual_source VALUES (?, ?, ?, 'direction', ?, ?, ?, 'user_origin_not_fact', ?, ?)",
                (manual_source_id, domain_label, account_ref.strip(), _canonical(instruction_source), actor.strip(), now, _hash({"source_kind": "direction", "original_content": instruction_source, "account_ref": account_ref.strip(), "evidence_role": "user_origin_not_fact"}), self.data_identity),
            )
            self.conn.execute(
                "INSERT INTO stage0_content_task VALUES (?, ?, 'research_plan', NULL, 'not_started', 0, ?, ?, ?, NULL)",
                (task_id, topic_version_id, self.data_identity, actor.strip(), now),
            )
            self.conn.execute(
                "INSERT INTO stage0_content_node_version VALUES (?, ?, 'formal_topic', NULL, NULL, NULL, 'approved', 'topic_directly_confirmed', 'passed', ?, ?, ?, 0)",
                (topic_version_id, task_id, self.data_identity, actor.strip(), now),
            )
            self._insert_artifact_payload(topic_version_id, "formal_topic", topic_payload)
            self._decision(task_id, "formal_topic", topic_version_id, "approved", actor.strip(), actor_kind, "direct user instruction is the formal-topic confirmation")
            self.conn.execute(
                "INSERT INTO stage0_manual_source_link VALUES (?, ?, 'formal_topic', ?, ?, ?)",
                (_id("manual_source_link"), manual_source_id, task_id, self.data_identity, now),
            )
            for attached_source_id in existing_manual_source_ids:
                self.conn.execute(
                    "INSERT INTO stage0_manual_source_link VALUES (?, ?, 'formal_topic', ?, ?, ?)",
                    (_id("manual_source_link"), attached_source_id, task_id, self.data_identity, now),
                )
            result = {"task_id": task_id, "topic_version_id": topic_version_id, "manual_source_id": manual_source_id, "current_node": "research_plan"}
            self._receipt("create_direct_formal_topic", idempotency_key, request, result)
            self._audit(task_id, "direct_formal_topic_confirmed_and_research_plan_queued", result)
        return result

    def record_research_material(
        self,
        *,
        task_id: str,
        source_ref: str,
        title: str,
        evidence_role: str,
        material: dict[str, Any],
        collected_at: str,
    ) -> dict[str, str]:
        """Retain a traceable external research source before the deep-research model step may use it."""
        task = self._task(task_id)
        if task["current_node"] != "deep_research" or task["current_status"] not in {"not_started", "processing"}:
            raise StateTransitionError("research material may only be retained for the active deep-research step")
        if evidence_role not in {"fact_evidence", "professional_interpretation", "audience_perception", "research_clue"}:
            raise StateTransitionError("research material has an unsupported evidence role")
        normalized_source = source_ref.strip().lower()
        if any(
            host in normalized_source
            for host in (
                "douyin.com",
                "tiktok.com",
                "bilibili.com",
                "xiaohongshu.com",
                "kuaishou.com",
            )
        ):
            raise StateTransitionError(
                "video-platform links cannot be retained as formal research evidence"
            )
        if not normalized_source.startswith(("https://", "http://")) or not title.strip() or not material or not collected_at.strip():
            raise StateTransitionError("research material requires a traceable external source, title, retained material and collection time")
        material_id = _id("research_material")
        with self.conn:
            self.conn.execute(
                "INSERT INTO stage0_research_material VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (material_id, task_id, source_ref.strip(), title.strip(), evidence_role, _canonical(material), _hash(material), self.data_identity, collected_at),
            )
        return {"research_material_id": material_id, "task_id": task_id, "source_ref": source_ref.strip()}

    def list_research_materials(self, *, task_id: str) -> list[dict[str, Any]]:
        self._task(task_id)
        rows = self.conn.execute(
            "SELECT research_material_id, source_ref, title, evidence_role, material_json, integrity_hash, collected_at "
            "FROM stage0_research_material WHERE task_id=? AND data_identity=? ORDER BY collected_at, research_material_id",
            (task_id, self.data_identity),
        ).fetchall()
        return [
            {"research_material_id": row["research_material_id"], "source_ref": row["source_ref"], "title": row["title"], "evidence_role": row["evidence_role"], "material": json.loads(row["material_json"]), "integrity_hash": row["integrity_hash"], "collected_at": row["collected_at"]}
            for row in rows
        ]

    def queue_research_plan_generation(
        self,
        *,
        task_id: str,
        actor: str,
        prompt_version: str,
        skill_version: str,
        model_config_version: str,
        idempotency_key: str,
    ) -> dict[str, str]:
        """Start the configured research-plan model step from an already confirmed topic; it never starts research itself."""
        task = self._task(task_id)
        self._assert_current_node(task, "research_plan", "not_started")
        topic_version_id = str(task["topic_version_id"])
        topic = self.get_artifact_payload(topic_version_id)["payload"]
        user_requirements = _canonical(
            {
                "core_question": topic.get("core_question"), "scope_or_requirement": topic.get("scope_or_requirement"),
                "material_gaps": topic.get("material_gaps", []), "risks": topic.get("risks", []),
                "timeliness": topic.get("timeliness", {}),
            }
        )
        assembly = InputAssembly(
            task_id=task_id, node="research_plan", upstream_version_id=topic_version_id,
            user_requirements=user_requirements,
            material_refs=tuple(topic.get("source_refs", [])) + tuple(topic.get("known_materials", [])),
            research_refs=(), content_plan_ref=None, considered_experience=(), adopted_experience=(), rejected_experience=(), omitted_materials=(),
            prompt_version=prompt_version, skill_version=skill_version, model_config_version=model_config_version,
        )
        assembly_result = self.create_input_assembly(assembly, idempotency_key=f"{idempotency_key}:assembly")
        request_result = self.create_node_request(
            task_id=task_id, node="research_plan", input_assembly_id=assembly_result["assembly_id"], actor=actor,
            idempotency_key=f"{idempotency_key}:request",
        )
        return {"task_id": task_id, "assembly_id": assembly_result["assembly_id"], "node_version_id": request_result["node_version_id"], "status": "research_plan_generating"}

    def submit_voice_profile(
        self,
        *,
        voice_profile_id: str,
        profile_label: str,
        reference_audio_ref: str,
        emotion_reference_audio_ref: str | None,
        mode: str,
        prompt_text: str,
        emotion_prompt_text: str,
        settings: dict[str, Any],
        actor: str,
        actor_kind: str,
        idempotency_key: str,
    ) -> dict[str, str]:
        """Retain a proposed voice configuration; it is unusable until a user confirms it."""
        if actor_kind != "user" or not actor.strip() or not voice_profile_id.strip() or not profile_label.strip():
            raise StateTransitionError("voice profile submission requires an explicit user identity and label")
        if not reference_audio_ref.strip() or mode not in {"basic", "ultimate"}:
            raise StateTransitionError("voice profile requires a reference audio and supported mode")
        if mode == "ultimate" and not (emotion_prompt_text or prompt_text).strip():
            raise StateTransitionError("ultimate voice mode requires the matching reference transcript")
        allowed_settings = {
            "style_prompt", "emotion_preset", "emotion_strength", "segment_strategy", "max_chars",
            "silence_ms", "cfg_value", "inference_timesteps",
        }
        if set(settings) != allowed_settings:
            raise StateTransitionError("voice profile settings do not match the controlled production contract")
        if settings["segment_strategy"] not in {"auto", "segment"}:
            raise StateTransitionError("formal voice profiles may not force one unsegmented long generation")
        request = {
            "voice_profile_id": voice_profile_id, "profile_label": profile_label,
            "reference_audio_ref": reference_audio_ref,
            "emotion_reference_audio_ref": emotion_reference_audio_ref, "mode": mode,
            "prompt_text": prompt_text, "emotion_prompt_text": emotion_prompt_text, "settings": settings,
            "actor": actor,
        }
        replay = self._replay("submit_voice_profile", idempotency_key, request)
        if replay:
            return replay
        now = _now()
        with self.conn:
            self.conn.execute(
                "INSERT INTO stage0_voice_profile VALUES (?, ?, ?, ?, ?, ?, ?, ?, 'awaiting_human_confirmation', ?, ?, ?, NULL, NULL)",
                (
                    voice_profile_id.strip(), profile_label.strip(), reference_audio_ref.strip(),
                    str(emotion_reference_audio_ref or "").strip() or None, mode, prompt_text.strip(),
                    emotion_prompt_text.strip(), _canonical(settings), self.data_identity, actor.strip(), now,
                ),
            )
            result = {"voice_profile_id": voice_profile_id.strip(), "status": "awaiting_human_confirmation"}
            self._receipt("submit_voice_profile", idempotency_key, request, result)
            self._audit(None, "voice_profile_awaiting_human_confirmation", result)
        return result

    def confirm_voice_profile(
        self, *, voice_profile_id: str, actor: str, actor_kind: str, idempotency_key: str
    ) -> dict[str, str]:
        if actor_kind != "user" or not actor.strip():
            raise StateTransitionError("voice profile activation requires an explicit user decision")
        profile = self.conn.execute(
            "SELECT * FROM stage0_voice_profile WHERE voice_profile_id=? AND data_identity=?",
            (voice_profile_id, self.data_identity),
        ).fetchone()
        if profile is None or profile["status"] != "awaiting_human_confirmation":
            raise StateTransitionError("voice profile is not awaiting confirmation")
        request = {"voice_profile_id": voice_profile_id, "actor": actor}
        replay = self._replay("confirm_voice_profile", idempotency_key, request)
        if replay:
            return replay
        now = _now()
        with self.conn:
            self.conn.execute(
                "UPDATE stage0_voice_profile SET status='active', confirmed_by=?, confirmed_at=? WHERE voice_profile_id=?",
                (actor.strip(), now, voice_profile_id),
            )
            result = {"voice_profile_id": voice_profile_id, "status": "active"}
            self._receipt("confirm_voice_profile", idempotency_key, request, result)
            self._audit(None, "voice_profile_activated_by_user", result)
        return result

    def get_voice_profile(self, *, voice_profile_id: str) -> dict[str, Any]:
        row = self.conn.execute(
            "SELECT * FROM stage0_voice_profile WHERE voice_profile_id=? AND data_identity=?",
            (voice_profile_id, self.data_identity),
        ).fetchone()
        if row is None:
            raise StateTransitionError("voice profile does not exist in this data identity")
        result = {key: row[key] for key in row.keys()}
        result["settings"] = json.loads(result.pop("settings_json"))
        return result

    def list_voice_profiles(self) -> list[dict[str, Any]]:
        rows = self.conn.execute(
            "SELECT * FROM stage0_voice_profile WHERE data_identity=? "
            "ORDER BY submitted_at DESC, voice_profile_id",
            (self.data_identity,),
        ).fetchall()
        results: list[dict[str, Any]] = []
        for row in rows:
            item = {key: row[key] for key in row.keys()}
            item["settings"] = json.loads(str(item.pop("settings_json")))
            results.append(item)
        return results

    def begin_audio_production(
        self,
        *,
        task_id: str,
        approved_content_version_id: str,
        voice_profile_id: str,
        title: str,
        script_text: str,
        actor: str,
        idempotency_key: str,
    ) -> dict[str, str]:
        task, version = self._task(task_id), self._version(approved_content_version_id)
        if (
            task["current_node"] != "user_final_confirmation" or task["current_status"] != "approved"
            or task["current_version_id"] != approved_content_version_id or version["node"] != "review"
        ):
            raise StateTransitionError("audio production requires the currently user-approved final review version")
        approval = self.conn.execute(
            "SELECT 1 FROM stage0_content_decision WHERE task_id=? AND version_id=? AND decision='approved' AND actor_kind='user' AND data_identity=?",
            (task_id, approved_content_version_id, self.data_identity),
        ).fetchone()
        profile = self.get_voice_profile(voice_profile_id=voice_profile_id)
        if approval is None or profile["status"] != "active" or not title.strip() or not script_text.strip() or not actor.strip():
            raise StateTransitionError("audio production requires approved content, an active voice profile, title, script and actor")
        request = {
            "task_id": task_id, "approved_content_version_id": approved_content_version_id,
            "voice_profile_id": voice_profile_id, "title": title, "script_text_hash": _hash(script_text.strip()),
            "actor": actor,
        }
        replay = self._replay("begin_audio_production", idempotency_key, request)
        if replay:
            return replay
        attempt = int(self.conn.execute(
            "SELECT COALESCE(MAX(attempt_number), 0) FROM stage0_audio_production WHERE task_id=? AND approved_content_version_id=? AND data_identity=?",
            (task_id, approved_content_version_id, self.data_identity),
        ).fetchone()[0]) + 1
        audio_production_id, now = _id("audio_production"), _now()
        with self.conn:
            self.conn.execute(
                "INSERT INTO stage0_audio_production VALUES (?, ?, ?, ?, ?, ?, ?, 'processing', NULL, NULL, '{}', '{}', '{}', NULL, ?, ?, ?, ?)",
                (
                    audio_production_id, task_id, approved_content_version_id, voice_profile_id, attempt,
                    title.strip(), _hash(script_text.strip()), self.data_identity, actor.strip(), now, now,
                ),
            )
            result = {"audio_production_id": audio_production_id, "status": "processing", "attempt_number": str(attempt)}
            self._receipt("begin_audio_production", idempotency_key, request, result)
            self._audit(task_id, "audio_production_started", result)
        return result

    def complete_audio_production(
        self,
        *,
        audio_production_id: str,
        audio_ref: str,
        metadata_ref: str,
        synthesis: dict[str, Any],
        quality_review: dict[str, Any],
        actor: str,
    ) -> dict[str, str]:
        production = self.conn.execute(
            "SELECT * FROM stage0_audio_production WHERE audio_production_id=? AND data_identity=?",
            (audio_production_id, self.data_identity),
        ).fetchone()
        if production is None or production["status"] != "processing":
            raise StateTransitionError("audio production is not processing")
        if not audio_ref.strip() or not metadata_ref.strip() or not actor.strip():
            raise StateTransitionError("completed audio production requires formal audio, metadata and actor")
        if quality_review.get("technical_status") != "passed" or quality_review.get("asr_status") != "passed":
            raise StateTransitionError("audio may reach human review only after technical and ASR checks run successfully")
        now = _now()
        with self.conn:
            self.conn.execute(
                "UPDATE stage0_audio_production SET status='awaiting_human_review', audio_ref=?, metadata_ref=?, synthesis_json=?, quality_review_json=?, updated_at=? WHERE audio_production_id=?",
                (audio_ref.strip(), metadata_ref.strip(), _canonical(synthesis), _canonical(quality_review), now, audio_production_id),
            )
            self._audit(production["task_id"], "audio_awaiting_human_review", {
                "audio_production_id": audio_production_id,
                "text_consistency_status": quality_review.get("text_consistency_status"),
            })
        return {"audio_production_id": audio_production_id, "status": "awaiting_human_review"}

    def fail_audio_production(self, *, audio_production_id: str, error: dict[str, Any], actor: str) -> dict[str, str]:
        production = self.conn.execute(
            "SELECT * FROM stage0_audio_production WHERE audio_production_id=? AND data_identity=?",
            (audio_production_id, self.data_identity),
        ).fetchone()
        if production is None or production["status"] != "processing" or not error or not actor.strip():
            raise StateTransitionError("only a processing audio attempt may record a formal failure")
        with self.conn:
            self.conn.execute(
                "UPDATE stage0_audio_production SET status='failed', failure_json=?, updated_at=? WHERE audio_production_id=?",
                (_canonical(error), _now(), audio_production_id),
            )
            self._audit(production["task_id"], "audio_production_failed", {"audio_production_id": audio_production_id})
        return {"audio_production_id": audio_production_id, "status": "failed"}

    def review_audio_production(
        self,
        *,
        audio_production_id: str,
        decision: str,
        issue_scope: str,
        reason: str,
        actor: str,
        actor_kind: str,
    ) -> dict[str, str]:
        production = self.conn.execute(
            "SELECT * FROM stage0_audio_production WHERE audio_production_id=? AND data_identity=?",
            (audio_production_id, self.data_identity),
        ).fetchone()
        if production is None or production["status"] != "awaiting_human_review":
            raise StateTransitionError("audio production is not awaiting human review")
        if actor_kind != "user" or not actor.strip() or not reason.strip():
            raise StateTransitionError("audio review requires an explicit user decision and reason")
        if decision == "approved" and issue_scope != "none":
            raise StateTransitionError("approved audio cannot carry a return scope")
        if decision == "returned" and issue_scope not in {"audio_regeneration", "content_revision"}:
            raise StateTransitionError("returned audio must identify audio or content revision scope")
        if decision not in {"approved", "returned"}:
            raise StateTransitionError("unsupported audio review decision")
        now = _now()
        with self.conn:
            self.conn.execute(
                "INSERT INTO stage0_audio_decision VALUES (?, ?, ?, ?, ?, ?, 'user', ?, ?)",
                (_id("audio_decision"), audio_production_id, decision, issue_scope, reason.strip(), actor.strip(), self.data_identity, now),
            )
            if decision == "approved":
                delivery_id = _id("audio_delivery")
                self.conn.execute(
                    "INSERT INTO stage0_audio_delivery VALUES (?, ?, ?, ?, 'delivered', ?, ?, ?)",
                    (
                        delivery_id, production["task_id"], production["approved_content_version_id"],
                        production["audio_ref"], self.data_identity, actor.strip(), now,
                    ),
                )
                self.conn.execute(
                    "UPDATE stage0_audio_production SET status='approved', audio_delivery_id=?, updated_at=? WHERE audio_production_id=?",
                    (delivery_id, now, audio_production_id),
                )
                result = {"audio_production_id": audio_production_id, "status": "approved", "audio_delivery_id": delivery_id}
                self._audit(production["task_id"], "audio_delivered_text_and_audio_only", result)
            else:
                self.conn.execute(
                    "UPDATE stage0_audio_production SET status='returned', updated_at=? WHERE audio_production_id=?",
                    (now, audio_production_id),
                )
                if issue_scope == "content_revision":
                    version = self._version(production["approved_content_version_id"])
                    if version["node"] != "review" or not version["upstream_version_id"]:
                        raise StateTransitionError("audio content issue cannot locate the approved review upstream")
                    task = self._task(production["task_id"])
                    self._set_task(
                        production["task_id"], node="review", version_id=version["upstream_version_id"],
                        status="not_started", revision=int(task["task_revision"]) + 1,
                    )
                result = {"audio_production_id": audio_production_id, "status": "returned", "issue_scope": issue_scope}
                self._audit(production["task_id"], "audio_returned_by_user", result)
        return result

    def get_audio_production(self, *, audio_production_id: str) -> dict[str, Any]:
        row = self.conn.execute(
            "SELECT * FROM stage0_audio_production WHERE audio_production_id=? AND data_identity=?",
            (audio_production_id, self.data_identity),
        ).fetchone()
        if row is None:
            raise StateTransitionError("audio production does not exist in this data identity")
        result = {key: row[key] for key in row.keys()}
        for key in ("synthesis_json", "quality_review_json", "failure_json"):
            result[key.removesuffix("_json")] = json.loads(result.pop(key))
        return result

    def propose_human_decision_carrier(
        self,
        *,
        carrier_binding_id: str,
        carrier_kind: str,
        entry_ref: str,
        context_strategy: str,
        actor: str,
    ) -> dict[str, str]:
        """Retain a carrier proposal without treating an untested transport as operational."""
        if not all(str(value).strip() for value in (carrier_binding_id, carrier_kind, entry_ref, context_strategy, actor)):
            raise StateTransitionError("human decision carrier proposal requires complete identity and context information")
        now = _now()
        with self.conn:
            self.conn.execute(
                "INSERT INTO stage0_human_decision_carrier_binding VALUES (?, ?, ?, ?, 'proposed', '{}', ?, ?, ?, NULL, NULL)",
                (
                    carrier_binding_id.strip(), carrier_kind.strip(), entry_ref.strip(), context_strategy.strip(),
                    self.data_identity, actor.strip(), now,
                ),
            )
            self._audit(None, "human_decision_carrier_proposed", {
                "carrier_binding_id": carrier_binding_id.strip(), "carrier_kind": carrier_kind.strip(),
            })
        return {"carrier_binding_id": carrier_binding_id.strip(), "status": "proposed"}

    def validate_human_decision_carrier(
        self,
        *,
        carrier_binding_id: str,
        validation_evidence: dict[str, Any],
        actor: str,
        actor_kind: str,
    ) -> dict[str, str]:
        """Activate a transport only after a user accepts real round-trip and identity evidence."""
        required = {
            "inbound_round_trip", "outbound_round_trip", "same_context_verified",
            "decision_identity_verified", "evidence_ref",
        }
        if set(validation_evidence) != required or any(
            validation_evidence[key] is not True for key in required - {"evidence_ref"}
        ) or not str(validation_evidence["evidence_ref"]).strip():
            raise StateTransitionError("human decision carrier validation requires complete real round-trip evidence")
        if actor_kind != "user" or not actor.strip():
            raise StateTransitionError("human decision carrier validation requires an explicit user decision")
        binding = self.conn.execute(
            "SELECT * FROM stage0_human_decision_carrier_binding WHERE carrier_binding_id=? AND data_identity=?",
            (carrier_binding_id, self.data_identity),
        ).fetchone()
        if binding is None or binding["status"] != "proposed":
            raise StateTransitionError("human decision carrier is not awaiting validation")
        now = _now()
        with self.conn:
            self.conn.execute(
                "UPDATE stage0_human_decision_carrier_binding SET status='validated', validation_evidence_json=?, validated_by=?, validated_at=? WHERE carrier_binding_id=?",
                (_canonical(validation_evidence), actor.strip(), now, carrier_binding_id),
            )
            self._audit(None, "human_decision_carrier_validated_by_user", {"carrier_binding_id": carrier_binding_id})
        return {"carrier_binding_id": carrier_binding_id, "status": "validated"}

    def list_validated_human_decision_carriers(self) -> list[dict[str, Any]]:
        rows = self.conn.execute(
            "SELECT * FROM stage0_human_decision_carrier_binding WHERE status='validated' AND data_identity=? ORDER BY validated_at, carrier_binding_id",
            (self.data_identity,),
        ).fetchall()
        result = []
        for row in rows:
            item = {key: row[key] for key in row.keys()}
            item["validation_evidence"] = json.loads(item.pop("validation_evidence_json"))
            result.append(item)
        return result

    def receive_human_decision_command(
        self,
        *,
        command_id: str,
        carrier_binding_id: str,
        session_ref: str,
        action: str,
        target_ref: str,
        payload: dict[str, Any],
        actor: str,
        actor_kind: str,
    ) -> dict[str, Any]:
        if actor_kind != "user" or not all(str(value).strip() for value in (command_id, session_ref, action, target_ref, actor)):
            raise StateTransitionError("formal human command requires a user, session, action and target")
        binding = self.conn.execute(
            "SELECT status FROM stage0_human_decision_carrier_binding WHERE carrier_binding_id=? AND data_identity=?",
            (carrier_binding_id, self.data_identity),
        ).fetchone()
        if binding is None or binding["status"] != "validated":
            raise StateTransitionError("formal human command requires a validated carrier binding")
        existing = self.conn.execute(
            "SELECT * FROM stage0_human_decision_command WHERE command_id=? AND data_identity=?",
            (command_id, self.data_identity),
        ).fetchone()
        canonical_payload = _canonical(payload)
        if existing is not None:
            if (
                existing["carrier_binding_id"] != carrier_binding_id or existing["session_ref"] != session_ref
                or existing["action"] != action or existing["target_ref"] != target_ref
                or existing["payload_json"] != canonical_payload or existing["actor"] != actor
            ):
                raise StateTransitionError("human command id was replayed with different content")
            return {key: existing[key] for key in existing.keys()}
        now = _now()
        with self.conn:
            self.conn.execute(
                "INSERT INTO stage0_human_decision_command VALUES (?, ?, ?, ?, ?, ?, ?, 'user', 'received', '{}', '{}', ?, ?, NULL)",
                (
                    command_id.strip(), carrier_binding_id, session_ref.strip(), action.strip(), target_ref.strip(),
                    canonical_payload, actor.strip(), self.data_identity, now,
                ),
            )
            self._audit(None, "formal_human_decision_command_received", {
                "command_id": command_id.strip(), "action": action.strip(), "target_ref": target_ref.strip(),
            })
        return {"command_id": command_id.strip(), "status": "received"}

    def finish_human_decision_command(
        self, *, command_id: str, status: str, result: dict[str, Any], error: dict[str, Any]
    ) -> dict[str, Any]:
        command = self.conn.execute(
            "SELECT * FROM stage0_human_decision_command WHERE command_id=? AND data_identity=?",
            (command_id, self.data_identity),
        ).fetchone()
        if command is None or command["status"] != "received" or status not in {"completed", "rejected"}:
            raise StateTransitionError("human decision command is not open or has an unsupported completion status")
        if status == "completed" and (not result or error):
            raise StateTransitionError("completed human decision command requires a result and no error")
        if status == "rejected" and (not error or result):
            raise StateTransitionError("rejected human decision command requires an error and no result")
        now = _now()
        with self.conn:
            self.conn.execute(
                "UPDATE stage0_human_decision_command SET status=?, result_json=?, error_json=?, completed_at=? WHERE command_id=?",
                (status, _canonical(result), _canonical(error), now, command_id),
            )
            self._audit(None, "formal_human_decision_command_finished", {"command_id": command_id, "status": status})
        return {"command_id": command_id, "status": status, "result": result, "error": error}

    def record_audio_delivery(
        self,
        *,
        task_id: str,
        approved_content_version_id: str,
        audio_ref: str,
        approved_audio_production_id: str,
        actor: str,
    ) -> dict[str, str]:
        """Compatibility boundary: only a user-approved controlled production may become a delivery."""
        version = self._version(approved_content_version_id)
        if version["task_id"] != task_id or version["node"] not in {"formal_draft", "copy_optimization", "de_ai_revision", "review"} or not audio_ref.strip() or not actor.strip():
            raise StateTransitionError("audio delivery requires a task-owned approved content version and a formal audio reference")
        approval = self.conn.execute(
            "SELECT 1 FROM stage0_content_decision WHERE task_id=? AND version_id=? AND decision='approved' AND data_identity=?",
            (task_id, approved_content_version_id, self.data_identity),
        ).fetchone()
        if approval is None:
            raise StateTransitionError("audio delivery can only follow an explicitly approved content version")
        production = self.conn.execute(
            "SELECT * FROM stage0_audio_production WHERE audio_production_id=? AND data_identity=?",
            (approved_audio_production_id, self.data_identity),
        ).fetchone()
        if (
            production is None or production["status"] != "approved"
            or production["task_id"] != task_id
            or production["approved_content_version_id"] != approved_content_version_id
            or production["audio_ref"] != audio_ref.strip()
        ):
            raise StateTransitionError("formal audio delivery cannot bypass controlled production and user audio review")
        if production["audio_delivery_id"]:
            return {"audio_delivery_id": production["audio_delivery_id"], "status": "delivered"}
        audio_delivery_id = _id("audio_delivery")
        now = _now()
        with self.conn:
            self.conn.execute(
                "INSERT INTO stage0_audio_delivery VALUES (?, ?, ?, ?, 'delivered', ?, ?, ?)",
                (audio_delivery_id, task_id, approved_content_version_id, audio_ref.strip(), self.data_identity, actor.strip(), now),
            )
            self.conn.execute(
                "UPDATE stage0_audio_production SET audio_delivery_id=?, updated_at=? WHERE audio_production_id=?",
                (audio_delivery_id, now, approved_audio_production_id),
            )
            self._audit(task_id, "audio_delivered_text_and_audio_only", {"audio_delivery_id": audio_delivery_id})
        return {"audio_delivery_id": audio_delivery_id, "status": "delivered"}

    def record_discovery_source(
        self,
        *,
        run_id: str,
        domain_label: str,
        source_type: str,
        source_object_id: str,
        source_object_version: str,
        source_time: str,
        expires_at: str | None = None,
        payload: dict[str, Any],
        idempotency_key: str,
    ) -> dict[str, str]:
        run = self._discovery_run(run_id)
        if run["status"] != "processing":
            raise StateTransitionError("discovery sources may only be recorded while a run is processing")
        self._assert_discovery_source_provenance(
            domain_label=domain_label,
            source_type=source_type,
            source_object_id=source_object_id,
            source_object_version=source_object_version,
            source_time=source_time,
            payload=payload,
        )
        request = {
            "run_id": run_id, "domain_label": domain_label, "source_type": source_type,
            "source_object_id": source_object_id, "source_object_version": source_object_version,
            "source_time": source_time, "expires_at": expires_at, "payload": payload,
        }
        replay = self._replay("stage1b_record_discovery_source", idempotency_key, request)
        if replay:
            return replay
        source_version_id = _id("discovery_source")
        with self.conn:
            self.conn.execute(
                "INSERT INTO stage1b_source_version VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (source_version_id, run_id, domain_label, source_type, source_object_id, source_object_version,
                 source_time, expires_at, _canonical(payload), _hash(payload), self.data_identity, _now()),
            )
            result = {"source_version_id": source_version_id, "input_integrity_hash": _hash(payload)}
            self._receipt("stage1b_record_discovery_source", idempotency_key, request, result)
            self._audit(run_id, "stage1b_source_recorded", {**result, "source_type": source_type})
        return result

    def record_discovery_filter(
        self,
        *,
        source_version_id: str,
        outcome: str,
        reason_code: str,
        detail: dict[str, Any],
        idempotency_key: str,
    ) -> dict[str, str]:
        source = self._discovery_source(source_version_id)
        if outcome not in {"eligible", "excluded"}:
            raise StateTransitionError("discovery filter outcome is invalid")
        request = {"source_version_id": source_version_id, "outcome": outcome, "reason_code": reason_code, "detail": detail}
        replay = self._replay("stage1b_record_discovery_filter", idempotency_key, request)
        if replay:
            return replay
        result_id = _id("discovery_filter")
        with self.conn:
            self.conn.execute(
                "INSERT INTO stage1b_filter_result VALUES (?, ?, ?, ?, ?, ?, ?)",
                (result_id, source_version_id, outcome, reason_code, _canonical(detail), self.data_identity, _now()),
            )
            result = {"filter_result_id": result_id, "source_version_id": source_version_id, "outcome": outcome}
            self._receipt("stage1b_record_discovery_filter", idempotency_key, request, result)
            self._audit(source["run_id"], "stage1b_source_filtered", {**result, "reason_code": reason_code})
        return result

    def create_discovery_input_assembly(
        self,
        *,
        run_id: str,
        source_version_id: str,
        payload: dict[str, Any],
        prompt_version: str,
        skill_version: str,
        idempotency_key: str,
    ) -> dict[str, str]:
        run, source = self._discovery_run(run_id), self._discovery_source(source_version_id)
        if run["status"] != "processing" or source["run_id"] != run_id:
            raise StateTransitionError("discovery input must belong to an active run source")
        filter_row = self.conn.execute("SELECT outcome FROM stage1b_filter_result WHERE source_version_id=?", (source_version_id,)).fetchone()
        if filter_row is None or filter_row["outcome"] != "eligible":
            raise StateTransitionError("LLM input may only be assembled for deterministically eligible sources")
        route = self._resolve_discovery_model_route()
        stored_payload = dict(payload)
        stored_payload["model_binding"] = {
            "route_id": route.route_id,
            "provider_name": route.provider_name,
            "provider_ref": route.provider_ref,
            "model_name": route.model_name,
            "config_version": route.config_version,
            "config_hash": route.config_hash,
        }
        request = {"run_id": run_id, "source_version_id": source_version_id, "payload": stored_payload, "prompt_version": prompt_version, "skill_version": skill_version, "model_config_version": route.config_version}
        replay = self._replay("stage1b_create_discovery_input", idempotency_key, request)
        if replay:
            return replay
        assembly_id, integrity_hash = _id("discovery_assembly"), _hash(stored_payload)
        with self.conn:
            self.conn.execute(
                "INSERT INTO stage1b_input_assembly VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (assembly_id, run_id, source_version_id, _canonical(stored_payload), integrity_hash, prompt_version, skill_version, route.config_version, self.data_identity, _now()),
            )
            result = {"assembly_id": assembly_id, "input_integrity_hash": integrity_hash, "model_config_hash": route.config_hash}
            self._receipt("stage1b_create_discovery_input", idempotency_key, request, result)
            self._audit(run_id, "stage1b_input_assembly_created", result)
        return result

    def pending_hotspot_judgement(self, *, run_id: str) -> dict[str, Any]:
        """Return one frozen hotspot judgement that an authorized user may resubmit once."""
        run = self._discovery_run(run_id)
        context = self._discovery_context(run_id)
        allowed_modes = {"real_daily_validation"} if self.data_identity == "production" else {"test_isolated"}
        if run["status"] != "processing" or context["execution_mode"] not in allowed_modes:
            raise StateTransitionError("only a processing real daily hotspot run can resubmit its judgement")
        rows = self.conn.execute(
            "SELECT source.source_version_id, source.domain_label, source.payload_json, "
            "assembly.assembly_id, assembly.payload_json AS assembly_payload_json "
            "FROM stage1b_source_version source "
            "JOIN stage1b_input_assembly assembly ON assembly.source_version_id=source.source_version_id "
            "WHERE source.run_id=? AND source.source_type='hotspot' "
            "ORDER BY source.created_at, source.source_version_id",
            (run_id,),
        ).fetchall()
        if len(rows) != 1:
            raise StateTransitionError("resubmission requires exactly one frozen hotspot judgement input")
        row = rows[0]
        source_version_id = str(row["source_version_id"])
        for table in ("stage1b_model_run", "stage1b_candidate_version", "stage1b_candidate_absence", "stage1b_source_failure"):
            if self.conn.execute(
                f"SELECT 1 FROM {table} WHERE run_id=? AND source_version_id=?",
                (run_id, source_version_id),
            ).fetchone():
                raise StateTransitionError("a hotspot judgement with a recorded outcome cannot be resubmitted")
        input_payload = json.loads(str(row["assembly_payload_json"]))
        input_payload.pop("model_binding", None)
        return {
            "run_id": run_id,
            "source_version_id": source_version_id,
            "domain_label": str(row["domain_label"]),
            "source_payload": json.loads(str(row["payload_json"])),
            "assembly_id": str(row["assembly_id"]),
            "input_payload": input_payload,
        }

    def prepare_discovery_model_request(
        self,
        *,
        run_id: str,
        source_version_id: str,
        assembly_id: str,
        prompt: str,
        skill_name: str = "source_to_topic",
        skill_version: str | None = None,
        skill_hash: str | None = None,
        binding_name: str | None = None,
        binding_version: str | None = None,
        binding_hash: str | None = None,
        model_input_payload: dict[str, Any] | None = None,
    ) -> ModelRequest:
        run, source = self._discovery_run(run_id), self._discovery_source(source_version_id)
        assembly = self._discovery_assembly(assembly_id)
        if run["status"] != "processing" or source["run_id"] != run_id or assembly["run_id"] != run_id or assembly["source_version_id"] != source_version_id:
            raise StateTransitionError("discovery model request has stale or mismatched input")
        route = self._resolve_discovery_model_route()
        payload = json.loads(assembly["payload_json"])
        expected_binding = {
            "route_id": route.route_id,
            "provider_name": route.provider_name,
            "provider_ref": route.provider_ref,
            "model_name": route.model_name,
            "config_version": route.config_version,
            "config_hash": route.config_hash,
        }
        if payload.get("model_binding") != expected_binding or assembly["model_config_version"] != route.config_version:
            raise StaleResultError("discovery input assembly no longer matches the explicit model binding")
        binding_name = binding_name or route.route_name
        binding_version = binding_version or route.config_version
        binding_hash = binding_hash or route.config_hash
        return ModelRequest(
            route_name=route.route_name,
            prompt=prompt,
            input_payload=model_input_payload or payload,
            correlation_id=run_id,
            skill_name=skill_name,
            skill_version=skill_version or assembly["skill_version"],
            skill_hash=skill_hash,
            binding_name=binding_name,
            binding_version=binding_version,
            binding_hash=binding_hash,
            metadata={"stage1b_core": {"run_id": run_id, "source_version_id": source_version_id, "input_assembly_id": assembly_id, "data_identity": self.data_identity, "prompt_version": assembly["prompt_version"], "skill_version": skill_version or assembly["skill_version"], "expected_binding": {**expected_binding, "binding_name": binding_name, "binding_version": binding_version, "binding_hash": binding_hash}}},
        )

    def persist_discovery_model_envelope(self, envelope: ModelRunEnvelope, binding: dict[str, Any]) -> str:
        if binding.get("data_identity") != self.data_identity:
            raise DataIdentityError("ModelGateway discovery identity does not match Core identity")
        run_id = str(binding.get("run_id") or "")
        source_version_id = str(binding.get("source_version_id") or "")
        assembly_id = str(binding.get("input_assembly_id") or "")
        run, source, assembly = self._discovery_run(run_id), self._discovery_source(source_version_id), self._discovery_assembly(assembly_id)
        if run["status"] != "processing" or source["run_id"] != run_id or assembly["run_id"] != run_id or assembly["source_version_id"] != source_version_id:
            raise StaleResultError("discovery ModelGateway envelope belongs to stale input")
        route = self._resolve_discovery_model_route()
        expected_binding = {
            "route_id": route.route_id,
            "provider_name": route.provider_name,
            "provider_ref": route.provider_ref,
            "model_name": route.model_name,
            "config_version": route.config_version,
            "config_hash": route.config_hash,
        }
        expected_request_binding = dict(binding.get("expected_binding") or {})
        expected_model_binding = {key: expected_request_binding.get(key) for key in expected_binding}
        if expected_model_binding != expected_binding:
            raise ModelGatewayRequiredError("discovery ModelGateway request lacks the current explicit binding")
        expected_binding_name = str(expected_request_binding.get("binding_name") or route.route_name)
        expected_binding_version = str(expected_request_binding.get("binding_version") or route.config_version)
        expected_binding_hash = str(expected_request_binding.get("binding_hash") or route.config_hash)
        if (
            envelope.route_name != route.route_name
            or envelope.route_id != route.route_id
            or envelope.provider_name != route.provider_name
            or envelope.provider_ref != route.provider_ref
            or envelope.model_name != route.model_name
            or envelope.config_version != route.config_version
            or envelope.config_hash != route.config_hash
            or envelope.binding_name != expected_binding_name
            or envelope.binding_version != expected_binding_version
            or envelope.binding_hash != expected_binding_hash
        ):
            raise ModelGatewayRequiredError("discovery ModelGateway envelope does not match source_to_topic binding")
        model_run_id = _id("discovery_model_run")
        with self.conn:
            self.conn.execute(
                "INSERT INTO stage1b_model_run VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (model_run_id, run_id, source_version_id, assembly_id, envelope.status, envelope.correlation_id,
                 binding["prompt_version"], binding["skill_version"], envelope.route_id or envelope.route_name,
                 envelope.config_version, envelope.provider_ref, envelope.provider_name, envelope.model_name,
                 assembly["integrity_hash"], envelope.output_hash, "not_validated", _canonical(envelope.error or {}),
                 "not_retried", _canonical({"prompt_tokens": envelope.usage.prompt_tokens, "completion_tokens": envelope.usage.completion_tokens, "total_tokens": envelope.usage.total_tokens}),
                 _canonical(envelope.cost), envelope.duration_ms, self.data_identity, _now(), 1),
            )
            self._audit(run_id, "stage1b_model_gateway_envelope_recorded", {"model_run_id": model_run_id, "status": envelope.status})
        return model_run_id

    def record_discovery_model_validation_failure(
        self, *, model_run_id: str, reason: str, raw_model_output: str | None = None
    ) -> None:
        model_run = self._discovery_model_run(model_run_id)
        with self.conn:
            self.conn.execute(
                "UPDATE stage1b_model_run SET validation_status='failed', error_json=? WHERE model_run_id=?",
                (_canonical({"validation_error": reason, "raw_model_output": raw_model_output}), model_run_id),
            )
            self._audit(model_run["run_id"], "stage1b_model_output_validation_failed", {"model_run_id": model_run_id, "reason": reason})

    def record_discovery_source_failure(
        self,
        *,
        run_id: str,
        source_version_id: str,
        model_run_id: str | None,
        failure_stage: str,
        reason: str,
        raw_model_output: str | None,
        idempotency_key: str,
    ) -> dict[str, str]:
        """Leave a failed source as a failure, never as a zero-candidate result."""
        run, source = self._discovery_run(run_id), self._discovery_source(source_version_id)
        if run["status"] != "processing" or source["run_id"] != run_id:
            raise StateTransitionError("source failure must belong to an active source run")
        if model_run_id is not None:
            model_run = self._discovery_model_run(model_run_id)
            if model_run["run_id"] != run_id or model_run["source_version_id"] != source_version_id:
                raise ModelGatewayRequiredError("source failure model run does not belong to the source")
        request = {
            "run_id": run_id,
            "source_version_id": source_version_id,
            "model_run_id": model_run_id,
            "failure_stage": failure_stage,
            "reason": reason,
            "raw_model_output": raw_model_output,
        }
        replay = self._replay("stage1b_record_source_failure", idempotency_key, request)
        if replay:
            return replay
        failure_id = _id("discovery_source_failure")
        with self.conn:
            self.conn.execute(
                "INSERT INTO stage1b_source_failure VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (
                    failure_id, run_id, source_version_id, model_run_id, failure_stage, reason,
                    raw_model_output,
                    "available" if raw_model_output is not None else "not_available",
                    self.data_identity, _now(),
                ),
            )
            result = {"failure_id": failure_id, "failure_stage": failure_stage}
            self._receipt("stage1b_record_source_failure", idempotency_key, request, result)
            self._audit(run_id, "stage1b_source_failed", result)
        return result

    def record_discovery_no_candidate(
        self,
        *,
        run_id: str,
        source_version_id: str,
        model_run_id: str | None,
        reason_code: str,
        detail: dict[str, Any],
        idempotency_key: str,
    ) -> dict[str, str]:
        run, source = self._discovery_run(run_id), self._discovery_source(source_version_id)
        if run["status"] != "processing" or source["run_id"] != run_id:
            raise StateTransitionError("candidate absence must belong to an active source run")
        if model_run_id is not None:
            model_run = self._discovery_model_run(model_run_id)
            if model_run["run_id"] != run_id or model_run["source_version_id"] != source_version_id:
                raise ModelGatewayRequiredError("candidate absence model run does not belong to the source")
        request = {"run_id": run_id, "source_version_id": source_version_id, "model_run_id": model_run_id, "reason_code": reason_code, "detail": detail}
        replay = self._replay("stage1b_record_candidate_absence", idempotency_key, request)
        if replay:
            return replay
        absence_id = _id("candidate_absence")
        with self.conn:
            self.conn.execute(
                "INSERT INTO stage1b_candidate_absence VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
                (absence_id, run_id, source_version_id, model_run_id, reason_code, _canonical(detail), self.data_identity, _now()),
            )
            if model_run_id is not None:
                valid_business_absences = {
                    "model_returned_no_candidate",
                    "candidate_outside_domain_policy",
                    "candidate_material_insufficient",
                }
                validation_status = "passed" if reason_code in valid_business_absences else "failed"
                self.conn.execute("UPDATE stage1b_model_run SET validation_status=? WHERE model_run_id=?", (validation_status, model_run_id))
            result = {"absence_id": absence_id, "reason_code": reason_code}
            self._receipt("stage1b_record_candidate_absence", idempotency_key, request, result)
            self._audit(run_id, "stage1b_candidate_absent", result)
        return result

    def create_discovery_candidate(
        self,
        *,
        run_id: str,
        source_version_id: str,
        model_run_id: str,
        candidate_id: str,
        payload: dict[str, Any],
        candidate_domain_label: str | None = None,
        allow_multiple_from_model_run: bool = False,
        idempotency_key: str,
    ) -> dict[str, str]:
        run, source, model_run = self._discovery_run(run_id), self._discovery_source(source_version_id), self._discovery_model_run(model_run_id)
        if run["status"] != "processing" or source["run_id"] != run_id or model_run["run_id"] != run_id or model_run["source_version_id"] != source_version_id:
            raise StateTransitionError("candidate does not belong to the active source run")
        if model_run["status"] != "succeeded" or model_run["via_model_gateway"] != 1 or (
            model_run["validation_status"] != "not_validated"
            and not (allow_multiple_from_model_run and model_run["validation_status"] == "passed")
        ):
            raise ModelGatewayRequiredError("candidate requires one successful unconsumed ModelGateway run")
        forbidden_fields = {"score", "rank", "weight", "recommendation_score", "quality_rank"}
        if forbidden_fields & set(payload):
            raise StateTransitionError("discovery candidates must not contain business-ranking fields")
        if self.conn.execute("SELECT 1 FROM stage1b_candidate_absence WHERE run_id=? AND source_version_id=?", (run_id, source_version_id)).fetchone():
            raise StateTransitionError("a source recorded as zero-candidate cannot create a candidate")
        domain_label = candidate_domain_label or str(source["domain_label"])
        if domain_label not in formal_domain_labels():
            raise StateTransitionError("candidate domain is not a formal domain")
        request = {"run_id": run_id, "source_version_id": source_version_id, "model_run_id": model_run_id, "candidate_id": candidate_id, "payload": payload, "candidate_domain_label": domain_label, "allow_multiple_from_model_run": allow_multiple_from_model_run}
        replay = self._replay("stage1b_create_discovery_candidate", idempotency_key, request)
        if replay:
            return replay
        candidate_version_id = _id("candidate_version")
        with self.conn:
            now = _now()
            self.conn.execute(
                "INSERT INTO stage1b_candidate_version VALUES (?, ?, ?, ?, ?, NULL, ?, ?, ?, 'awaiting_user_decision', ?, ?)",
                (candidate_version_id, candidate_id, run_id, domain_label, source_version_id, model_run_id, _canonical(payload), _hash(payload), self.data_identity, now),
            )
            self.conn.execute(
                "INSERT INTO stage1b_candidate_support VALUES (?, ?, ?, 'initial_discovery', ?, ?, ?)",
                (_id("candidate_support"), candidate_version_id, source_version_id, "candidate first formed from this formal discovery source", self.data_identity, now),
            )
            self.conn.execute(
                "INSERT INTO stage1b_candidate_pool_state VALUES (?, ?, 'current', ?, 'candidate_pool_window_v1', ?, ?)",
                (_id("candidate_pool_state"), candidate_version_id, "candidate first entered the active pool", self.data_identity, now),
            )
            self.conn.execute("UPDATE stage1b_model_run SET validation_status='passed' WHERE model_run_id=?", (model_run_id,))
            result = {"candidate_version_id": candidate_version_id, "candidate_id": candidate_id}
            self._receipt("stage1b_create_discovery_candidate", idempotency_key, request, result)
            self._audit(run_id, "stage1b_candidate_created", result)
        return result

    def complete_discovery_run(
        self,
        *,
        run_id: str,
        domains: tuple[str, ...],
        lifecycle_status: str = "completed",
        failure_reason: str | None = None,
        idempotency_key: str,
    ) -> dict[str, str]:
        run = self._discovery_run(run_id)
        context = self._discovery_context(run_id)
        if lifecycle_status not in DISCOVERY_RUN_OUTCOMES - {"processing"}:
            raise StateTransitionError("discovery lifecycle status is invalid")
        if run["status"] == "completed":
            return {"run_id": run_id, "status": context["lifecycle_status"]}
        if run["status"] != "processing":
            raise StateTransitionError("only a processing discovery run can complete")
        request = {
            "run_id": run_id,
            "domains": list(domains),
            "lifecycle_status": lifecycle_status,
            "failure_reason": failure_reason,
        }
        replay = self._replay("stage1b_complete_discovery_run", idempotency_key, request)
        if replay:
            return replay
        with self.conn:
            for domain_label in domains:
                rows = self.conn.execute(
                    "SELECT candidate.candidate_version_id FROM stage1b_candidate_version candidate "
                    "JOIN stage1b_candidate_pool_state pool ON pool.pool_state_id=(SELECT newest.pool_state_id FROM stage1b_candidate_pool_state newest WHERE newest.candidate_version_id=candidate.candidate_version_id AND newest.data_identity=? ORDER BY newest.effective_at DESC, newest.pool_state_id DESC LIMIT 1) "
                    "LEFT JOIN stage1b_candidate_assessment_revision assessment ON assessment.assessment_revision_id=(SELECT newest.assessment_revision_id FROM stage1b_candidate_assessment_revision newest WHERE newest.candidate_version_id=candidate.candidate_version_id AND newest.data_identity=? ORDER BY newest.assessed_at DESC, newest.assessment_revision_id DESC LIMIT 1) "
                    "WHERE candidate.domain_label=? AND candidate.data_identity=? AND candidate.status='awaiting_user_decision' AND pool.pool_status='current' "
                    "AND NOT EXISTS (SELECT 1 FROM stage1b_candidate_decision decision WHERE decision.candidate_version_id=candidate.candidate_version_id) "
                    "ORDER BY assessment.total_score IS NULL, assessment.total_score DESC, candidate.created_at, candidate.candidate_version_id LIMIT ?",
                    (
                        self.data_identity,
                        self.data_identity,
                        domain_label,
                        self.data_identity,
                        DAILY_PRIORITY_REPORT_LIMIT,
                    ),
                ).fetchall()
                if rows:
                    for position, row in enumerate(rows, start=1):
                        snapshot_id = _id("snapshot")
                        self.conn.execute("INSERT INTO stage1b_daily_snapshot VALUES (?, ?, ?, ?, ?, ?, ?)", (snapshot_id, run_id, domain_label, row["candidate_version_id"], position, self.data_identity, _now()))
                else:
                    self.conn.execute("INSERT INTO stage1b_daily_snapshot VALUES (?, ?, ?, NULL, 0, ?, ?)", (_id("snapshot"), run_id, domain_label, self.data_identity, _now()))
            core_status = "completed" if lifecycle_status in {"completed", "completed_with_failures"} else "failed"
            completed_at = _now()
            self.conn.execute(
                "UPDATE stage1b_discovery_run SET status=?, completed_at=?, failure_reason=? WHERE run_id=?",
                (core_status, completed_at, failure_reason, run_id),
            )
            self.conn.execute(
                "UPDATE stage1b_run_execution_context SET lifecycle_status=? WHERE run_id=?",
                (lifecycle_status, run_id),
            )
            result = {"run_id": run_id, "status": lifecycle_status, "execution_mode": context["execution_mode"]}
            self._receipt("stage1b_complete_discovery_run", idempotency_key, request, result)
            self._audit(run_id, "stage1b_daily_snapshot_finalized", {**result, "failure_reason": failure_reason})
        return result

    def record_candidate_assessment(
        self,
        *,
        candidate_version_id: str,
        dimension_scores: dict[str, int | float],
        dimension_reasons: dict[str, str],
        assessed_by: str,
    ) -> dict[str, Any]:
        """Persist the six visible candidate dimensions; ranking is computed, never invented by a model field."""
        candidate = self._discovery_candidate(candidate_version_id)
        if candidate["status"] != "awaiting_user_decision" or not assessed_by.strip():
            raise StateTransitionError("only an undecided candidate may receive a scored assessment")
        if self._candidate_pool_state(candidate_version_id)["pool_status"] != "current":
            raise StateTransitionError("only a current-pool candidate may be scored; reactivate it with new same-angle material first")
        if set(dimension_scores) != set(CANDIDATE_SCORE_WEIGHTS) or set(dimension_reasons) != set(CANDIDATE_SCORE_WEIGHTS):
            raise StateTransitionError("candidate assessment requires all six agreed dimensions and reasons")
        normalized_scores: dict[str, float] = {}
        for key, value in dimension_scores.items():
            score = float(value)
            if score < 0 or score > 5:
                raise StateTransitionError("candidate dimension scores must stay between 0 and 5")
            normalized_scores[key] = score
            if not str(dimension_reasons[key]).strip():
                raise StateTransitionError("candidate assessment requires a reason for every dimension")
        total = sum(normalized_scores[key] * CANDIDATE_SCORE_WEIGHTS[key] / 5 for key in CANDIDATE_SCORE_WEIGHTS)
        with self.conn:
            self.conn.execute(
                "INSERT INTO stage1b_candidate_assessment_revision VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
                (_id("candidate_assessment"), candidate_version_id, _canonical(normalized_scores), _canonical(dimension_reasons), total, assessed_by.strip(), self.data_identity, _now()),
            )
        return {"candidate_version_id": candidate_version_id, "total_score": total, "dimension_scores": normalized_scores}

    def record_candidate_relation(
        self,
        *,
        candidate_version_id: str,
        related_candidate_version_id: str,
        relation_kind: str,
        relation_reason: str,
    ) -> dict[str, str]:
        """Keep same-topic material together without silently deleting a different valid angle."""
        if relation_kind not in {"same_topic_same_angle", "same_topic_different_angle"} or not relation_reason.strip():
            raise StateTransitionError("candidate relation requires an agreed kind and reason")
        self._discovery_candidate(candidate_version_id)
        self._discovery_candidate(related_candidate_version_id)
        with self.conn:
            relation_id = _id("candidate_relation")
            self.conn.execute(
                "INSERT INTO stage1b_candidate_relation VALUES (?, ?, ?, ?, ?, ?, ?)",
                (relation_id, candidate_version_id, related_candidate_version_id, relation_kind, relation_reason.strip(), self.data_identity, _now()),
            )
        return {"relation_id": relation_id, "candidate_version_id": candidate_version_id, "related_candidate_version_id": related_candidate_version_id, "relation_kind": relation_kind}

    def record_candidate_support(
        self,
        *,
        candidate_version_id: str,
        source_version_id: str,
        relation_reason: str,
    ) -> dict[str, str]:
        """Attach same-topic/same-angle material to the existing candidate and reactivate it when needed."""
        candidate = self._discovery_candidate(candidate_version_id)
        source = self._discovery_source(source_version_id)
        if candidate["status"] != "awaiting_user_decision" or candidate["domain_label"] != source["domain_label"]:
            raise StateTransitionError("candidate support must target an undecided candidate in the same domain")
        if not relation_reason.strip():
            raise StateTransitionError("candidate support requires a traceable same-angle reason")
        previous_state = self._candidate_pool_state(candidate_version_id)
        with self.conn:
            support_id = _id("candidate_support")
            self.conn.execute(
                "INSERT INTO stage1b_candidate_support VALUES (?, ?, ?, 'same_topic_same_angle_material', ?, ?, ?)",
                (support_id, candidate_version_id, source_version_id, relation_reason.strip(), self.data_identity, _now()),
            )
            if previous_state["pool_status"] == "historical":
                now = _now()
                previous_effective_at = datetime.fromisoformat(str(previous_state["effective_at"]).replace("Z", "+00:00"))
                current_effective_at = datetime.fromisoformat(now.replace("Z", "+00:00"))
                if current_effective_at <= previous_effective_at:
                    now = (previous_effective_at + timedelta(microseconds=1)).isoformat()
                self.conn.execute(
                    "INSERT INTO stage1b_candidate_pool_state VALUES (?, ?, 'current', ?, ?, ?, ?)",
                    (_id("candidate_pool_state"), candidate_version_id, "new same-topic same-angle source reactivated historical candidate", self._candidate_pool_parameter()["parameter_version"], self.data_identity, now),
                )
            self._audit(candidate["run_id"], "stage1b_candidate_support_recorded", {"candidate_version_id": candidate_version_id, "source_version_id": source_version_id, "support_id": support_id})
        return {"support_id": support_id, "candidate_version_id": candidate_version_id, "source_version_id": source_version_id, "pool_status": "current"}

    def count_candidate_support_materials(self, *, candidate_version_id: str) -> int:
        """Return retained same-angle materials so a new source can trigger a visible reassessment."""
        self._discovery_candidate(candidate_version_id)
        row = self.conn.execute(
            "SELECT COUNT(*) AS material_count FROM stage1b_candidate_support "
            "WHERE candidate_version_id=? AND data_identity=?",
            (candidate_version_id, self.data_identity),
        ).fetchone()
        return int(row["material_count"])

    def set_candidate_pool_observation_window(
        self,
        *,
        observation_window_days: int,
        actor: str,
        actor_kind: str,
        reason: str,
        effective_at: str,
    ) -> dict[str, Any]:
        """Version a human-approved future pool window without rewriting historical state."""
        if actor_kind != "user" or not actor.strip() or not reason.strip() or observation_window_days <= 0:
            raise StateTransitionError("candidate pool window requires an explicit user decision, positive days and reason")
        parameter_version = _id("candidate_pool_window")
        with self.conn:
            self.conn.execute(
                "INSERT INTO stage1b_candidate_pool_parameter_version VALUES (?, ?, ?, ?, ?, ?)",
                (parameter_version, observation_window_days, actor.strip(), reason.strip(), self.data_identity, effective_at),
            )
        return {"parameter_version": parameter_version, "observation_window_days": observation_window_days, "effective_at": effective_at}

    def move_expired_candidates_to_history(self, *, as_of: str) -> list[dict[str, Any]]:
        """Apply the confirmed two-week pool rule; it preserves all evidence and only removes expired items from ranking."""
        parameter = self._candidate_pool_parameter(as_of)
        cutoff = (datetime.fromisoformat(as_of.replace("Z", "+00:00")) - timedelta(days=int(parameter["observation_window_days"]))).isoformat()
        rows = self.conn.execute(
            "SELECT candidate.candidate_version_id, candidate.run_id FROM stage1b_candidate_version candidate "
            "JOIN stage1b_candidate_pool_state state ON state.pool_state_id=(SELECT newest.pool_state_id FROM stage1b_candidate_pool_state newest WHERE newest.candidate_version_id=candidate.candidate_version_id AND newest.data_identity=? ORDER BY newest.effective_at DESC, newest.pool_state_id DESC LIMIT 1) "
            "WHERE candidate.data_identity=? AND candidate.status='awaiting_user_decision' AND state.pool_status='current' "
            "AND candidate.created_at<=? "
            "AND NOT EXISTS (SELECT 1 FROM stage1b_daily_snapshot snapshot WHERE snapshot.candidate_version_id=candidate.candidate_version_id AND snapshot.display_position>0 AND snapshot.created_at>?) "
            "AND NOT EXISTS (SELECT 1 FROM stage1b_candidate_support support WHERE support.candidate_version_id=candidate.candidate_version_id AND support.created_at>?) "
            "AND NOT EXISTS (SELECT 1 FROM stage1b_candidate_support support JOIN stage1b_source_version source ON source.source_version_id=support.source_version_id WHERE support.candidate_version_id=candidate.candidate_version_id AND source.expires_at IS NOT NULL AND source.expires_at>?)",
            (self.data_identity, self.data_identity, cutoff, cutoff, cutoff, as_of),
        ).fetchall()
        moved: list[dict[str, Any]] = []
        with self.conn:
            for row in rows:
                state_id = _id("candidate_pool_state")
                self.conn.execute(
                    "INSERT INTO stage1b_candidate_pool_state VALUES (?, ?, 'historical', ?, ?, ?, ?)",
                    (state_id, row["candidate_version_id"], "outside report top 10 with no new support and no valid time window during observation period", parameter["parameter_version"], self.data_identity, as_of),
                )
                result = {"candidate_version_id": row["candidate_version_id"], "pool_status": "historical", "parameter_version": parameter["parameter_version"]}
                self._audit(row["run_id"], "stage1b_candidate_moved_to_history", result)
                moved.append(result)
        return moved

    def get_discovery_snapshot(self, *, run_id: str, domain_label: str) -> list[dict[str, Any]]:
        self._discovery_run(run_id)
        context = self._discovery_context(run_id)
        rows = self.conn.execute(
            "SELECT snapshot.display_position, candidate.candidate_version_id, candidate.candidate_id, candidate.payload_json, source.source_type, source.source_time, source.expires_at, source.payload_json AS source_payload_json, assessment.total_score, assessment.dimension_scores_json, assessment.dimension_reasons_json FROM stage1b_daily_snapshot snapshot LEFT JOIN stage1b_candidate_version candidate ON candidate.candidate_version_id=snapshot.candidate_version_id LEFT JOIN stage1b_source_version source ON source.source_version_id=candidate.source_version_id LEFT JOIN stage1b_candidate_assessment_revision assessment ON assessment.assessment_revision_id=(SELECT newest.assessment_revision_id FROM stage1b_candidate_assessment_revision newest WHERE newest.candidate_version_id=candidate.candidate_version_id AND newest.data_identity=? ORDER BY newest.assessed_at DESC, newest.assessment_revision_id DESC LIMIT 1) WHERE snapshot.run_id=? AND snapshot.domain_label=? ORDER BY snapshot.display_position, snapshot.snapshot_id",
            (self.data_identity, run_id, domain_label),
        ).fetchall()
        result: list[dict[str, Any]] = []
        for row in rows:
            if row["candidate_version_id"] is None:
                continue
            candidate_payload = json.loads(row["payload_json"])
            source_payload = json.loads(row["source_payload_json"])
            support_rows = self.conn.execute(
                "SELECT support.support_id, support.support_kind, support.relation_reason, support.created_at, source.source_version_id, source.source_type, source.source_time, source.expires_at, source.payload_json FROM stage1b_candidate_support support JOIN stage1b_source_version source ON source.source_version_id=support.source_version_id WHERE support.candidate_version_id=? AND support.data_identity=? ORDER BY support.created_at, support.support_id",
                (row["candidate_version_id"], self.data_identity),
            ).fetchall()
            support_materials = [
                {
                    "support_id": support["support_id"],
                    "kind": support["support_kind"],
                    "reason": support["relation_reason"],
                    "added_at": support["created_at"],
                    "source_version_id": support["source_version_id"],
                    "source_type": support["source_type"],
                    "source_time": support["source_time"],
                    "expires_at": support["expires_at"],
                    "source": json.loads(support["payload_json"]),
                }
                for support in support_rows
            ]
            relation_rows = self.conn.execute(
                "SELECT related_candidate_version_id, relation_kind, relation_reason FROM stage1b_candidate_relation WHERE candidate_version_id=? AND data_identity=? ORDER BY created_at, relation_id",
                (row["candidate_version_id"], self.data_identity),
            ).fetchall()
            source_reference = candidate_payload.get("source_reference")
            if not isinstance(source_reference, dict):
                source_reference = {
                    "source_version_id": None,
                    "source_type": row["source_type"],
                    "source_time": row["source_time"],
                }
            stage1a_handoff_packet = {
                "candidate_version_id": row["candidate_version_id"],
                "candidate_id": row["candidate_id"],
                "source_type": row["source_type"],
                "core_question": candidate_payload.get("core_question"),
                "domain_label": domain_label,
                "recommendation_reason": candidate_payload.get("why_attention") or candidate_payload.get("new_angle"),
                "source_evidence_refs": [
                    source_reference,
                    source_payload.get("formal_source", {}),
                ],
                "risk_limits": candidate_payload.get("risk_limits"),
                "material_gap": candidate_payload.get("material_readiness"),
                "timeliness_limits": row["expires_at"] or "not_time_limited",
                "user_confirmation_required": True,
            }
            result.append({"display_position": row["display_position"], "candidate_version_id": row["candidate_version_id"], "candidate_id": row["candidate_id"], "candidate": candidate_payload, "score": {"total": row["total_score"], "dimensions": json.loads(row["dimension_scores_json"]) if row["dimension_scores_json"] else None, "reasons": json.loads(row["dimension_reasons_json"]) if row["dimension_reasons_json"] else None}, "source_type": row["source_type"], "source_time": row["source_time"], "expires_at": row["expires_at"], "source": source_payload, "support_materials": support_materials, "related_candidates": [{"candidate_version_id": relation["related_candidate_version_id"], "kind": relation["relation_kind"], "reason": relation["relation_reason"]} for relation in relation_rows], "stage1a_handoff_packet": stage1a_handoff_packet, "execution_mode": context["execution_mode"], "lifecycle_status": context["lifecycle_status"], "formal_candidate_pool": context["execution_mode"] == "production_daily" and context["lifecycle_status"] == "completed"})
        return result

    def record_discovery_decision(
        self,
        *,
        candidate_version_id: str,
        decision: str,
        actor: str,
        reason: str,
        formal_topic_task_id: str | None,
        idempotency_key: str,
    ) -> dict[str, str]:
        candidate = self._discovery_candidate(candidate_version_id)
        if decision not in {"selected", "deferred", "rejected", "angle_change_requested", "evergreen"}:
            raise StateTransitionError("unsupported discovery decision")
        if decision == "selected":
            raise StateTransitionError("selected candidates must use select_discovery_candidate")
        if not reason.strip():
            raise StateTransitionError("a user decision requires a reason")
        if formal_topic_task_id is not None:
            raise StateTransitionError("only the atomic selection action may create a formal-topic link")
        if candidate["status"] != "awaiting_user_decision":
            raise StateTransitionError("candidate is not awaiting a user decision")
        request = {"candidate_version_id": candidate_version_id, "decision": decision, "actor": actor, "reason": reason, "formal_topic_task_id": formal_topic_task_id}
        replay = self._replay("stage1b_record_discovery_decision", idempotency_key, request)
        if replay:
            return replay
        if self.conn.execute("SELECT 1 FROM stage1b_candidate_decision WHERE candidate_version_id=?", (candidate_version_id,)).fetchone():
            raise StateTransitionError("candidate already has a user decision")
        with self.conn:
            decision_id = _id("candidate_decision")
            self.conn.execute("INSERT INTO stage1b_candidate_decision VALUES (?, ?, ?, ?, ?, ?, ?, ?)", (decision_id, candidate_version_id, decision, actor, reason, formal_topic_task_id, self.data_identity, _now()))
            result = {"decision_id": decision_id, "candidate_version_id": candidate_version_id, "decision": decision}
            self._receipt("stage1b_record_discovery_decision", idempotency_key, request, result)
            self._audit(candidate["run_id"], "stage1b_candidate_user_decision", result)
        return result

    def select_discovery_candidate(
        self,
        *,
        candidate_version_id: str,
        actor: str,
        actor_kind: str,
        reason: str,
        idempotency_key: str,
    ) -> dict[str, str]:
        """Atomically hand one exact Stage 1B candidate to Stage 1A awaiting confirmation."""
        if actor_kind != "user" or not actor.strip() or not reason.strip():
            raise StateTransitionError("candidate selection requires an explicit user and reason")
        candidate = self._discovery_candidate(candidate_version_id)
        run = self._discovery_run(candidate["run_id"])
        context = self._discovery_context(candidate["run_id"])
        if context["execution_mode"] != "production_daily" or context["lifecycle_status"] not in {"completed", "completed_with_failures"}:
            raise StateTransitionError("only a completed production_daily candidate may enter Stage 1A")
        if run["status"] != "completed" or candidate["status"] != "awaiting_user_decision":
            raise StateTransitionError("only a completed awaiting-user-decision candidate may be selected")
        if self.conn.execute("SELECT 1 FROM stage1b_candidate_decision WHERE candidate_version_id=?", (candidate_version_id,)).fetchone():
            raise StateTransitionError("candidate already has a user decision")
        payload = json.loads(candidate["payload_json"])
        domain_label = str(candidate["domain_label"])
        now = _now()
        if self.formal_topic_title_seen(domain_label=domain_label, normalized_title=str(payload.get("normalized_title") or "")):
            raise StateTransitionError("a matching formal topic already exists in this domain")
        source_ref = payload.get("source_reference")
        if not isinstance(source_ref, dict) or source_ref.get("source_version_id") != candidate["source_version_id"]:
            raise StateTransitionError("candidate source reference is not the exact candidate source version")
        support_refs = [
            {"kind": "candidate_support", "source_version_id": row["source_version_id"], "support_kind": row["support_kind"], "reason": row["relation_reason"]}
            for row in self.conn.execute(
                "SELECT source_version_id, support_kind, relation_reason FROM stage1b_candidate_support WHERE candidate_version_id=? AND data_identity=? ORDER BY created_at, support_id",
                (candidate_version_id, self.data_identity),
            ).fetchall()
        ]
        request = {"candidate_version_id": candidate_version_id, "actor": actor, "actor_kind": actor_kind, "reason": reason}
        replay = self._replay("stage1b_select_discovery_candidate", idempotency_key, request)
        if replay:
            return replay
        task_id, topic_version_id, decision_id = _id("task"), _id("version"), _id("candidate_decision")
        topic_payload = {
            "title": payload["title"],
            "core_question": payload["core_question"],
            "domain": domain_label,
            "source_refs": [source_ref, *support_refs, {"kind": "daily_candidate", "candidate_version_id": candidate_version_id}],
        }
        with self.conn:
            self.conn.execute(
                "INSERT INTO stage0_content_task VALUES (?, ?, 'research_plan', NULL, 'not_started', 0, ?, ?, ?, NULL)",
                (task_id, topic_version_id, self.data_identity, actor, now),
            )
            self.conn.execute(
                "INSERT INTO stage0_content_node_version VALUES (?, ?, 'formal_topic', NULL, NULL, NULL, 'approved', 'topic_selected_from_candidate', 'passed', ?, ?, ?, 0)",
                (topic_version_id, task_id, self.data_identity, actor, now),
            )
            self._insert_artifact_payload(topic_version_id, "formal_topic", topic_payload)
            self._decision(task_id, "formal_topic", topic_version_id, "approved", actor, actor_kind, "candidate selection is the formal-topic confirmation")
            self.conn.execute(
                "INSERT INTO stage1b_candidate_decision VALUES (?, ?, 'selected', ?, ?, ?, ?, ?)",
                (decision_id, candidate_version_id, actor, reason, task_id, self.data_identity, now),
            )
            result = {"task_id": task_id, "topic_version_id": topic_version_id, "decision_id": decision_id, "candidate_version_id": candidate_version_id}
            self._receipt("stage1b_select_discovery_candidate", idempotency_key, request, result)
            self._audit(candidate["run_id"], "stage1b_candidate_selected_and_research_plan_queued", result)
            self._audit(task_id, "formal_topic_confirmed_from_stage1b_candidate", result)
        return result

    def latest_stage1_production_handoff(self) -> dict[str, str]:
        """Return the latest completed production-daily candidate handoff for Stage 1 evidence."""
        row = self.conn.execute(
            "SELECT run.run_id, candidate.candidate_version_id, decision.decision_id, decision.formal_topic_task_id "
            "FROM stage1b_candidate_decision decision "
            "JOIN stage1b_candidate_version candidate ON candidate.candidate_version_id=decision.candidate_version_id "
            "JOIN stage1b_discovery_run run ON run.run_id=candidate.run_id "
            "JOIN stage1b_run_execution_context context ON context.run_id=run.run_id "
            "WHERE decision.decision='selected' AND decision.formal_topic_task_id IS NOT NULL "
            "AND run.status='completed' AND context.execution_mode='production_daily' "
            "AND context.lifecycle_status IN ('completed', 'completed_with_failures') "
            "ORDER BY decision.created_at DESC LIMIT 1"
        ).fetchone()
        if row is None:
            raise StateTransitionError(
                "Stage 1 closure requires a completed production_daily candidate selected by the user and handed off to Stage 1A"
            )
        return {
            "run_id": row["run_id"],
            "candidate_version_id": row["candidate_version_id"],
            "decision_id": row["decision_id"],
            "formal_topic_task_id": row["formal_topic_task_id"],
        }

    def discovery_run_diagnostics(self, *, run_id: str) -> dict[str, Any]:
        """Return the persisted outcome of one discovery run without changing it."""
        run = self.conn.execute(
            "SELECT run.run_id, run.status, run.failure_reason, context.execution_mode, context.lifecycle_status "
            "FROM stage1b_discovery_run run "
            "JOIN stage1b_run_execution_context context ON context.run_id=run.run_id "
            "WHERE run.run_id=? AND run.data_identity=?",
            (run_id, self.data_identity),
        ).fetchone()
        if run is None:
            raise StateTransitionError("discovery run was not found in this data identity")
        candidates = self.conn.execute(
            "SELECT candidate.candidate_version_id, candidate.domain_label, source.source_type, candidate.payload_json "
            "FROM stage1b_candidate_version candidate "
            "JOIN stage1b_source_version source ON source.source_version_id=candidate.source_version_id "
            "WHERE candidate.run_id=? ORDER BY candidate.created_at, candidate.candidate_version_id",
            (run_id,),
        ).fetchall()
        absences = self.conn.execute(
            "SELECT source.source_type, source.source_object_id, absence.reason_code, absence.detail_json "
            "FROM stage1b_candidate_absence absence "
            "JOIN stage1b_source_version source ON source.source_version_id=absence.source_version_id "
            "WHERE absence.run_id=? ORDER BY absence.created_at, absence.absence_id",
            (run_id,),
        ).fetchall()
        failures = self.conn.execute(
            "SELECT source.source_type, source.source_object_id, failure.failure_stage, failure.reason, "
            "failure.raw_model_output, failure.raw_model_output_status "
            "FROM stage1b_source_failure failure "
            "JOIN stage1b_source_version source ON source.source_version_id=failure.source_version_id "
            "WHERE failure.run_id=? ORDER BY failure.created_at, failure.failure_id",
            (run_id,),
        ).fetchall()
        source_ledger = self.conn.execute(
            "SELECT source.source_version_id, source.source_type, source.source_object_id, source.source_object_version, "
            "source.source_time, source.payload_json, filter.outcome AS filter_outcome, filter.reason_code AS filter_reason, "
            "filter.detail_json AS filter_detail, model.model_run_id, model.status AS model_status, "
            "model.validation_status AS model_validation_status, model.error_json AS model_error, "
            "assembly.payload_json AS assembly_payload, assembly.integrity_hash AS assembly_integrity_hash, "
            "candidate.candidate_version_id, candidate.payload_json AS candidate_payload, "
            "absence.reason_code AS absence_reason, absence.detail_json AS absence_detail, "
            "failure.failure_stage, failure.reason AS failure_reason, failure.raw_model_output, "
            "failure.raw_model_output_status "
            "FROM stage1b_source_version source "
            "LEFT JOIN stage1b_filter_result filter ON filter.source_version_id=source.source_version_id "
            "LEFT JOIN stage1b_input_assembly assembly ON assembly.source_version_id=source.source_version_id "
            "LEFT JOIN stage1b_model_run model ON model.source_version_id=source.source_version_id "
            "LEFT JOIN stage1b_candidate_version candidate ON candidate.source_version_id=source.source_version_id "
            "LEFT JOIN stage1b_candidate_absence absence ON absence.source_version_id=source.source_version_id "
            "LEFT JOIN stage1b_source_failure failure ON failure.source_version_id=source.source_version_id "
            "WHERE source.run_id=? ORDER BY source.created_at, source.source_version_id",
            (run_id,),
        ).fetchall()
        searched_topics = self.conn.execute(
            "SELECT tag.tag, tag.tag_id, tag.domain_label, cursor.last_searched_at "
            "FROM domain_search_cursor cursor JOIN domain_search_tags tag ON tag.tag_id=cursor.tag_id "
            "WHERE cursor.run_id=? ORDER BY tag.domain_label, tag.tag_id",
            (run_id,),
        ).fetchall()
        source_history_rows = self.conn.execute(
            "SELECT prior.source_type, prior.source_object_id, prior.source_object_version, prior.run_id, prior.created_at, "
            "context.execution_mode, context.lifecycle_status, filter.outcome, filter.reason_code "
            "FROM stage1b_source_version prior "
            "JOIN stage1b_run_execution_context context ON context.run_id=prior.run_id "
            "LEFT JOIN stage1b_filter_result filter ON filter.source_version_id=prior.source_version_id "
            "WHERE prior.data_identity=? AND prior.run_id<>? "
            "AND EXISTS (SELECT 1 FROM stage1b_source_version current "
            "WHERE current.run_id=? AND current.source_type=prior.source_type "
            "AND current.source_object_id=prior.source_object_id "
            "AND current.source_object_version=prior.source_object_version) "
            "ORDER BY prior.created_at, prior.run_id",
            (self.data_identity, run_id, run_id),
        ).fetchall()
        source_history: dict[tuple[str, str, str], list[dict[str, Any]]] = {}
        for row in source_history_rows:
            source_history.setdefault(
                (row["source_type"], row["source_object_id"], row["source_object_version"]), []
            ).append({
                "run_id": row["run_id"],
                "created_at": row["created_at"],
                "execution_mode": row["execution_mode"],
                "lifecycle_status": row["lifecycle_status"],
                "filter_outcome": row["outcome"],
                "filter_reason": row["reason_code"],
            })
        return {
            "run_id": run["run_id"],
            "execution_mode": run["execution_mode"],
            "status": run["status"],
            "lifecycle_status": run["lifecycle_status"],
            "failure_reason": run["failure_reason"],
            "searched_topics": [
                {"topic": row["tag"], "topic_id": row["tag_id"], "domain_label": row["domain_label"], "searched_at": row["last_searched_at"]}
                for row in searched_topics
            ],
            "candidates": [
                {
                    "candidate_version_id": row["candidate_version_id"],
                    "domain_label": row["domain_label"],
                    "source_type": row["source_type"],
                    "title": json.loads(row["payload_json"])["title"],
                }
                for row in candidates
            ],
            "absences": [
                {
                    "source_type": row["source_type"],
                    "source_object_id": row["source_object_id"],
                    "reason_code": row["reason_code"],
                    "detail": json.loads(row["detail_json"]),
                }
                for row in absences
            ],
            "failures": [
                {
                    "source_type": row["source_type"],
                    "source_object_id": row["source_object_id"],
                    "failure_stage": row["failure_stage"],
                    "reason": row["reason"],
                    "raw_model_output": row["raw_model_output"],
                    "raw_model_output_status": row["raw_model_output_status"],
                }
                for row in failures
            ],
            "source_ledger": [
                {
                    "source_version_id": row["source_version_id"],
                    "source_type": row["source_type"],
                    "source_object_id": row["source_object_id"],
                    "source_object_version": row["source_object_version"],
                    "source_time": row["source_time"],
                    "source": json.loads(row["payload_json"]),
                    "filter": {
                        "outcome": row["filter_outcome"],
                        "reason": row["filter_reason"],
                        "detail": json.loads(row["filter_detail"]) if row["filter_detail"] else None,
                    },
                    "model_input": {
                        "payload": json.loads(row["assembly_payload"]) if row["assembly_payload"] else None,
                        "integrity_hash": row["assembly_integrity_hash"],
                    },
                    "model": {
                        "model_run_id": row["model_run_id"],
                        "status": row["model_status"],
                        "validation_status": row["model_validation_status"],
                        "error": json.loads(row["model_error"]) if row["model_error"] else None,
                        "raw_output": row["raw_model_output"],
                        "raw_output_status": row["raw_model_output_status"] or "not_available",
                    },
                    "candidate": json.loads(row["candidate_payload"]) if row["candidate_payload"] else None,
                    "absence": {
                        "reason": row["absence_reason"],
                        "detail": json.loads(row["absence_detail"]) if row["absence_detail"] else None,
                    } if row["absence_reason"] else None,
                    "failure": {
                        "stage": row["failure_stage"],
                        "reason": row["failure_reason"],
                        "raw_model_output": row["raw_model_output"],
                        "raw_model_output_status": row["raw_model_output_status"],
                    } if row["failure_stage"] else None,
                    "prior_records": source_history.get(
                        (row["source_type"], row["source_object_id"], row["source_object_version"]), []
                    ),
                }
                for row in source_ledger
            ],
        }

    def record_daily_competitor_snapshot(
        self,
        *,
        account_id: str,
        items: tuple[dict[str, Any], ...],
        raw_archive_ref: str,
        collection_run_id: str,
        observed_at: datetime | None = None,
    ) -> dict[str, Any]:
        """Retain one real daily creator snapshot through the formal Core boundary."""
        account = self.conn.execute(
            "SELECT * FROM competitor_accounts WHERE account_id=? AND registration_status='active'",
            (account_id,),
        ).fetchone()
        if account is None:
            raise StateTransitionError("daily competitor snapshot requires an active registered account")
        if str(account["platform"]) != "douyin":
            raise StateTransitionError("daily competitor snapshot currently supports Douyin only")
        if not raw_archive_ref.strip() or not collection_run_id.strip():
            raise StateTransitionError("daily competitor snapshot requires retained raw evidence and a collection run identity")
        now = observed_at or datetime.now(timezone.utc)
        if now.tzinfo is None:
            now = now.replace(tzinfo=timezone.utc)

        def published_time(value: Any) -> datetime | None:
            if value is None or value == "":
                return None
            if isinstance(value, (int, float)) or (isinstance(value, str) and value.strip().isdigit()):
                numeric = float(value)
                if numeric > 10_000_000_000:
                    numeric /= 1000
                try:
                    return datetime.fromtimestamp(numeric, tz=timezone.utc)
                except (OverflowError, OSError, ValueError):
                    return None
            try:
                parsed = datetime.fromisoformat(str(value).strip().replace("Z", "+00:00"))
            except ValueError:
                return None
            return parsed if parsed.tzinfo else parsed.replace(tzinfo=timezone.utc)

        inserted = updated = checks_recorded = 0
        source_refs: list[dict[str, str]] = []
        with self.conn:
            for item in items:
                if not isinstance(item, dict):
                    raise StateTransitionError("daily competitor snapshot item must be an object")
                source_id = str(item.get("source_id") or "").strip()
                title = str(item.get("title") or "").strip()
                url = str(item.get("url") or "").strip()
                if not source_id or not url:
                    raise StateTransitionError("daily competitor snapshot item lacks its platform identity or URL")
                metrics = item.get("metrics") if isinstance(item.get("metrics"), dict) else {}
                counts = {
                    key: int(metrics.get(key) or 0)
                    for key in ("like_count", "comment_count", "share_count", "collect_count")
                }
                published = published_time(item.get("published_at"))
                publish_iso = published.isoformat() if published is not None else None
                video_id = "competitor_video_" + _hash({"account_id": account_id, "source_id": source_id})[:20]
                existing = self.conn.execute(
                    "SELECT * FROM competitor_videos WHERE account_id=? AND platform_item_id=?",
                    (account_id, source_id),
                ).fetchone()
                if existing is None:
                    delay_hours = round((now - published).total_seconds() / 3600, 2) if published is not None else None
                    if delay_hours is None:
                        first_contact_category = "formal_new"
                    elif delay_hours <= 36:
                        first_contact_category = "formal_new"
                    elif delay_hours < HISTORICAL_MATURITY_DAYS * 24:
                        first_contact_category = "transition"
                    else:
                        first_contact_category = "historical_mature"
                    self.conn.execute(
                        "INSERT INTO competitor_videos("
                        "video_id, account_id, platform, platform_item_id, title, url, publish_time, duration_sec, "
                        "like_count, comment_count, share_count, collect_count, first_contact_category, discovery_delay_hours, "
                        "excluded_reason, registration_run_id, raw_archive_ref, raw_json"
                        ") VALUES (?, ?, 'douyin', ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                        (
                            video_id, account_id, source_id, title, url, publish_iso,
                            int(item.get("duration_seconds") or 0), counts["like_count"], counts["comment_count"],
                            counts["share_count"], counts["collect_count"], first_contact_category, delay_hours,
                            None if published is not None else "missing_publish_time", collection_run_id,
                            raw_archive_ref, _canonical(item),
                        ),
                    )
                    if first_contact_category in {"formal_new", "transition"}:
                        check_index = 0 if first_contact_category == "formal_new" else None
                        day_since_publish = (
                            None
                            if first_contact_category == "formal_new" or delay_hours is None
                            else max(0, int(delay_hours // 24))
                        )
                        self.conn.execute(
                            "INSERT OR IGNORE INTO video_checks("
                            "check_id, video_id, discovery_batch_index, day_since_publish, like_count, comment_count, "
                            "share_count, collect_count, run_id) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
                            (
                                "video_check_" + _hash({
                                    "video_id": video_id,
                                    "d_index": check_index,
                                    "day_since_publish": day_since_publish,
                                })[:20],
                                video_id, check_index, day_since_publish, counts["like_count"],
                                counts["comment_count"], counts["share_count"],
                                counts["collect_count"], collection_run_id,
                            ),
                        )
                        checks_recorded += 1
                    inserted += 1
                else:
                    category = str(existing["first_contact_category"] or "")
                    tracking_completed = int(existing["tracking_completed"] or 0)
                    mature_at = existing["mature_snapshot_taken_at"]
                    self.conn.execute(
                        "UPDATE competitor_videos SET title=?, url=?, publish_time=COALESCE(?, publish_time), duration_sec=?, "
                        "like_count=?, comment_count=?, share_count=?, collect_count=?, raw_archive_ref=?, raw_json=?, "
                        "last_checked_at=CURRENT_TIMESTAMP, check_count=check_count+1 WHERE video_id=?",
                        (
                            title, url, publish_iso, int(item.get("duration_seconds") or 0), counts["like_count"],
                            counts["comment_count"], counts["share_count"], counts["collect_count"], raw_archive_ref,
                            _canonical(item), existing["video_id"],
                        ),
                    )
                    check_index: int | None = None
                    day_since_publish: int | None = None
                    if category == "formal_new" and not tracking_completed:
                        first_seen = published_time(existing["first_seen_at"]) or now
                        check_index = min(
                            7,
                            max(0, (now.date() - first_seen.astimezone(timezone.utc).date()).days),
                        )
                    elif category == "transition" and mature_at is None:
                        existing_published = published_time(existing["publish_time"]) or published
                        if existing_published is not None:
                            day_since_publish = max((now - existing_published).days, 0)
                            if day_since_publish >= 7:
                                self.conn.execute(
                                    "UPDATE competitor_videos SET mature_snapshot_taken_at=? WHERE video_id=?",
                                    (now.isoformat(), existing["video_id"]),
                                )
                    if check_index is not None or day_since_publish is not None:
                        check_identity = (
                            {"video_id": existing["video_id"], "d_index": check_index}
                            if check_index is not None
                            else {"video_id": existing["video_id"], "day_since_publish": day_since_publish}
                        )
                        cursor = self.conn.execute(
                            "INSERT OR IGNORE INTO video_checks("
                            "check_id, video_id, discovery_batch_index, day_since_publish, like_count, comment_count, "
                            "share_count, collect_count, run_id) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
                            (
                                "video_check_" + _hash(check_identity)[:20],
                                existing["video_id"], check_index, day_since_publish, counts["like_count"],
                                counts["comment_count"], counts["share_count"], counts["collect_count"], collection_run_id,
                            ),
                        )
                        checks_recorded += int(cursor.rowcount > 0)
                        if category == "formal_new":
                            completed_points = int(self.conn.execute(
                                "SELECT COUNT(DISTINCT discovery_batch_index) FROM video_checks "
                                "WHERE video_id=? AND discovery_batch_index BETWEEN 0 AND 7",
                                (existing["video_id"],),
                            ).fetchone()[0])
                            if completed_points == 8:
                                self.conn.execute(
                                    "UPDATE competitor_videos SET tracking_completed=1 WHERE video_id=?",
                                    (existing["video_id"],),
                                )
                    updated += 1
                source_refs.append({"video_id": video_id, "platform_item_id": source_id})
            result = {
                "account_id": account_id,
                "collection_run_id": collection_run_id,
                "inserted": inserted,
                "updated": updated,
                "checks_recorded": checks_recorded,
                "source_refs": source_refs,
            }
            self._audit(None, "daily_competitor_snapshot_recorded", result)
        return result

    def judge_daily_competitor_hits(
        self,
        *,
        account_id: str,
        source_video_ids: tuple[str, ...],
        run_id: str,
    ) -> dict[str, Any]:
        """Apply the same small cold-start contract after every daily snapshot."""
        if not source_video_ids or not run_id.strip():
            return {"account_id": account_id, "judged": 0, "new_hit_ids": []}
        account = self.conn.execute(
            "SELECT * FROM competitor_accounts WHERE account_id=? AND registration_status='active'",
            (account_id,),
        ).fetchone()
        if account is None:
            raise StateTransitionError("daily hit judgement requires one active competitor account")

        mature_rows = self.conn.execute(
            "SELECT * FROM competitor_videos WHERE account_id=? AND excluded_reason IS NULL "
            "AND publish_time IS NOT NULL AND datetime(publish_time)<=datetime('now', ?) "
            "ORDER BY datetime(publish_time) DESC, video_id DESC",
            (account_id, f"-{HISTORICAL_MATURITY_DAYS} days"),
        ).fetchall()
        recent_cutoff = datetime.now(timezone.utc) - timedelta(days=MATURE_HISTORY_WINDOW_DAYS)
        def normalized_publish_time(row: sqlite3.Row) -> datetime:
            value = datetime.fromisoformat(str(row["publish_time"]).replace("Z", "+00:00"))
            return value if value.tzinfo is not None else value.replace(tzinfo=timezone.utc)

        recent = [
            row for row in mature_rows
            if normalized_publish_time(row) >= recent_cutoff
        ][:FIRST_REGISTRATION_MAX_ITEMS]
        if len(recent) < MIN_RELIABLE_HISTORY_ITEMS:
            known = {str(row["video_id"]) for row in recent}
            older = [row for row in mature_rows if str(row["video_id"]) not in known]
            recent.extend(older[: max(0, MIN_RELIABLE_HISTORY_ITEMS - len(recent))])
        baseline_rows = recent[:FIRST_REGISTRATION_MAX_ITEMS]
        baseline_metrics = [
            {
                metric: int(row[metric] or 0)
                for metric in HISTORICAL_METRICS
            }
            for row in baseline_rows
        ]
        mature_reference = judge_against_mature_history(
            candidate_metrics={metric: 0 for metric in HISTORICAL_METRICS},
            baseline_metrics=baseline_metrics,
        )
        baseline_group_id = "mature_baseline_" + _hash({
            "account_id": account_id,
            "run_id": run_id,
            "samples": [str(row["video_id"]) for row in baseline_rows],
        })[:20]

        new_hit_ids: list[str] = []
        judged = 0
        with self.conn:
            if mature_reference["baseline_active"]:
                for metric, median in mature_reference["medians"].items():
                    self.conn.execute(
                        "INSERT OR IGNORE INTO baselines(baseline_id, account_id, baseline_mode, metric, observation_point, "
                        "sample_count, median_value, run_id) VALUES (?, ?, 'mature_history', ?, NULL, ?, ?, ?)",
                        (
                            f"{baseline_group_id}_{metric}",
                            account_id,
                            metric,
                            len(baseline_rows),
                            median,
                            run_id,
                        ),
                    )
            for video_id in source_video_ids:
                video = self.conn.execute(
                    "SELECT * FROM competitor_videos WHERE video_id=? AND account_id=?",
                    (video_id, account_id),
                ).fetchone()
                if video is None or video["excluded_reason"] is not None:
                    continue
                metrics = {
                    metric: int(video[metric] or 0)
                    for metric in HISTORICAL_METRICS
                }
                checks = self.conn.execute(
                    "SELECT * FROM video_checks WHERE video_id=? ORDER BY checked_at, check_id",
                    (video_id,),
                ).fetchall()
                latest = checks[-1] if checks else None
                category = str(video["first_contact_category"] or "")
                observation = "history"
                channels: list[str] = []
                baseline_mode: str | None = None
                baseline_id: str | None = None

                ratio_result = judge_against_mature_history(
                    candidate_metrics=metrics,
                    baseline_metrics=[],
                )
                ratio_channels = [
                    channel for channel in ratio_result["channels"]
                    if channel.startswith("comment_like_ratio:")
                ]
                channels.extend(ratio_channels)

                if category in {"historical_mature", "transition"}:
                    history_result = judge_against_mature_history(
                        candidate_metrics=metrics,
                        baseline_metrics=baseline_metrics,
                    )
                    channels.extend(
                        channel for channel in history_result["channels"]
                        if not channel.startswith("comment_like_ratio:")
                    )
                    if any(not channel.startswith("comment_like_ratio:") for channel in channels):
                        baseline_mode = "mature_history"
                        baseline_id = baseline_group_id
                    if latest is not None and latest["day_since_publish"] is not None:
                        observation = f"day{int(latest['day_since_publish'])}"
                elif category == "formal_new" and latest is not None:
                    d_index = latest["discovery_batch_index"]
                    if d_index is not None:
                        observation = f"D{int(d_index)}"
                        predecessor_rows = self.conn.execute(
                            "SELECT video_id FROM competitor_videos WHERE account_id=? "
                            "AND first_contact_category='formal_new' AND tracking_completed=1 "
                            "AND discovery_delay_hours<=36 AND video_id<>? "
                            "ORDER BY first_seen_at, video_id",
                            (account_id, video_id),
                        ).fetchall()
                        sequences: list[dict[int, dict[str, int]]] = []
                        for predecessor in predecessor_rows:
                            sequence_rows = self.conn.execute(
                                "SELECT discovery_batch_index, like_count, comment_count, share_count, collect_count "
                                "FROM video_checks WHERE video_id=? AND discovery_batch_index BETWEEN 0 AND 7 "
                                "ORDER BY discovery_batch_index, checked_at",
                                (predecessor["video_id"],),
                            ).fetchall()
                            sequence = {
                                int(row["discovery_batch_index"]): {
                                    metric: int(row[metric] or 0)
                                    for metric in HISTORICAL_METRICS
                                }
                                for row in sequence_rows
                            }
                            if set(sequence) >= set(range(8)):
                                sequences.append(sequence)
                        formal_result = judge_against_formal_d_baseline(
                            candidate_metrics=metrics,
                            predecessor_sequences=sequences,
                            discovery_batch_index=int(d_index),
                        )
                        channels.extend(formal_result["channels"])
                        if formal_result["baseline_active"]:
                            baseline_mode = "formal_d_series"
                            baseline_id = "formal_d_baseline_" + _hash({
                                "account_id": account_id,
                                "run_id": run_id,
                                "observation": observation,
                            })[:20]
                            for metric, median in formal_result["medians"].items():
                                self.conn.execute(
                                    "INSERT OR IGNORE INTO baselines(baseline_id, account_id, baseline_mode, metric, "
                                    "observation_point, sample_count, median_value, run_id) "
                                    "VALUES (?, ?, 'formal_d_series', ?, ?, ?, ?, ?)",
                                    (
                                        f"{baseline_id}_{metric}",
                                        account_id,
                                        metric,
                                        observation,
                                        formal_result["qualifying_sequence_count"],
                                        median,
                                        run_id,
                                    ),
                                )
                        elif int(d_index) >= 7:
                            rough_result = judge_against_mature_history(
                                candidate_metrics=metrics,
                                baseline_metrics=baseline_metrics,
                            )
                            channels.extend(
                                channel for channel in rough_result["channels"]
                                if not channel.startswith("comment_like_ratio:")
                            )
                            if any(
                                not channel.startswith("comment_like_ratio:")
                                for channel in rough_result["channels"]
                            ):
                                baseline_mode = "mature_history"
                                baseline_id = baseline_group_id

                channels = list(dict.fromkeys(channels))
                judged += 1
                if not channels:
                    continue
                confidence = (
                    "formal"
                    if any(
                        channel.startswith(("formal_d_", "comment_like_ratio:"))
                        for channel in channels
                    )
                    else "rough"
                )
                previous_rules = []
                if video["trigger_rules"]:
                    try:
                        previous_rules = list(json.loads(str(video["trigger_rules"])))
                    except (TypeError, ValueError, json.JSONDecodeError):
                        previous_rules = []
                cumulative = list(dict.fromkeys([*previous_rules, *channels]))
                self.conn.execute(
                    "UPDATE competitor_videos SET first_trigger_observation=COALESCE(first_trigger_observation, ?), "
                    "first_trigger_at=COALESCE(first_trigger_at, ?), trigger_rules=?, peak_observation=?, "
                    "baseline_mode=COALESCE(baseline_mode, ?), judgment_confidence=? WHERE video_id=?",
                    (
                        observation,
                        _now(),
                        _canonical(cumulative),
                        observation,
                        baseline_mode,
                        confidence,
                        video_id,
                    ),
                )
                hit_id = "competitor_hit_" + _hash({
                    "account_id": account_id,
                    "source_id": str(video["platform_item_id"]),
                })[:20]
                cursor = self.conn.execute(
                    "INSERT OR IGNORE INTO hits(hit_id, video_id, account_id, platform, platform_item_id, title, url, "
                    "publish_time, like_count, comment_count, share_count, collect_count, hit_channel, "
                    "judgment_confidence, baseline_id, run_id, preparation_status) "
                    "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 'pending')",
                    (
                        hit_id,
                        video_id,
                        account_id,
                        video["platform"],
                        video["platform_item_id"],
                        video["title"],
                        video["url"],
                        video["publish_time"],
                        metrics["like_count"],
                        metrics["comment_count"],
                        metrics["share_count"],
                        metrics["collect_count"],
                        ",".join(channels),
                        confidence,
                        baseline_id,
                        run_id,
                    ),
                )
                if cursor.rowcount:
                    new_hit_ids.append(hit_id)
        result = {
            "account_id": account_id,
            "judged": judged,
            "mature_baseline_sample_count": len(baseline_rows),
            "new_hit_ids": new_hit_ids,
        }
        self._audit(None, "daily_competitor_hits_judged", result)
        return result

    def discovery_source_readiness(
        self, *, domain_label: str, daily_since: str, hotspot_discovery_run_id: str | None = None
    ) -> dict[str, Any]:
        if domain_label not in formal_domain_labels():
            raise StateTransitionError("daily discovery requires a configured formal domain")
        tables = {row["name"] for row in self.conn.execute("SELECT name FROM sqlite_master WHERE type='table'").fetchall()}
        competitor_ready = {"competitor_accounts", "competitor_videos", "hits"}.issubset(tables)
        daily_sources = self.conn.execute(
            "SELECT COUNT(*) FROM competitor_videos video JOIN competitor_accounts account ON account.account_id=video.account_id "
            "WHERE account.domain_label=? AND account.registration_status='active' AND COALESCE(account.source_config_ref, '')<>'' "
            "AND video.publish_time>=? AND COALESCE(video.title, '')<>'' AND COALESCE(video.url, '')<>'' "
            "AND COALESCE(video.raw_archive_ref, '')<>'' AND video.excluded_reason IS NULL",
            (domain_label, daily_since),
        ).fetchone()[0] if competitor_ready else 0
        historical_sources = self.conn.execute(
            "SELECT COUNT(*) FROM hits hit JOIN competitor_videos video ON video.video_id=hit.video_id "
            "JOIN competitor_accounts account ON account.account_id=hit.account_id WHERE account.domain_label=? "
            "AND account.registration_status='active' AND COALESCE(account.source_config_ref, '')<>'' "
            "AND hit.judgment_confidence IN ('rough', 'formal') AND COALESCE(hit.title, '')<>'' AND COALESCE(hit.url, '')<>'' "
            "AND COALESCE(video.raw_archive_ref, '')<>'' AND video.excluded_reason IS NULL",
            (domain_label,),
        ).fetchone()[0] if competitor_ready else 0
        hotspot_scope = ""
        hotspot_params: list[Any] = [daily_since]
        if hotspot_discovery_run_id is not None:
            hotspot_scope = " AND run.discovery_run_id=?"
            hotspot_params.append(hotspot_discovery_run_id)
        hotspot_rows = self.conn.execute(
            "SELECT observation.* FROM trendradar_hotspot_observation observation "
            "JOIN trendradar_collection_run run ON run.collection_run_id=observation.collection_run_id "
            "WHERE observation.observed_at>=? AND run.status IN ('completed', 'completed_with_failures')"
            + hotspot_scope,
            tuple(hotspot_params),
        ).fetchall()
        hotspot_sources = len(
            _build_hotspot_event_cluster_sources(
                list(hotspot_rows),
                per_source_limit=len(hotspot_rows) or 1,
            )
        )
        tag_sources = self.conn.execute(
            "SELECT COUNT(*) FROM discovered_external_videos video JOIN domain_search_tags tag ON tag.tag_id=video.tag_id "
            "WHERE video.domain_label=? AND tag.status='active' AND COALESCE(video.title, '')<>'' AND COALESCE(video.url, '')<>''",
            (domain_label,),
        ).fetchone()[0]
        question_expansion_sources = self.conn.execute(
            "SELECT COUNT(*) FROM stage1_question_expansion_source WHERE domain_label=? "
            "AND validation_outcome='supported' AND data_identity=?",
            (domain_label, self.data_identity),
        ).fetchone()[0]
        saved_user_direction_sources = self.conn.execute(
            "SELECT COUNT(*) FROM stage1_saved_user_direction_source WHERE domain_label=? "
            "AND status='active' AND data_identity=?",
            (domain_label, self.data_identity),
        ).fetchone()[0]
        counts = {
            "hotspot_sources": hotspot_sources,
            "daily_sources": daily_sources,
            "historical_sources": historical_sources,
            "tag_sources": tag_sources,
            "question_expansion_sources": question_expansion_sources,
            "saved_user_direction_sources": saved_user_direction_sources,
        }
        if not any(counts.values()):
            return {"status": "blocked", "reason": "no_qualified_formal_source", **counts}
        return {"status": "ready", "reason": "qualified_formal_source_available", **counts}

    def load_hotspot_event_clusters(
        self,
        *,
        daily_since: str,
        per_source_limit: int | None,
        hotspot_discovery_run_id: str | None = None,
    ) -> list[dict[str, Any]]:
        """Return one temporary source per TrendRadar event, without domain keyword filtering."""
        hotspot_scope = ""
        hotspot_params = [daily_since]
        if hotspot_discovery_run_id is not None:
            hotspot_scope = " AND run.discovery_run_id=? "
            hotspot_params.append(hotspot_discovery_run_id)
        rows = self.conn.execute(
            "SELECT observation.* FROM trendradar_hotspot_observation observation "
            "JOIN trendradar_collection_run run ON run.collection_run_id=observation.collection_run_id "
            "WHERE observation.observed_at>=? AND run.status IN ('completed', 'completed_with_failures') "
            + hotspot_scope
            + "ORDER BY observation.observed_at DESC, observation.observation_id ASC",
            tuple(hotspot_params),
        ).fetchall()
        return _build_hotspot_event_cluster_sources(
            list(rows), per_source_limit=per_source_limit
        )

    def reusable_hotspot_batch_status(self, *, discovery_run_id: str) -> dict[str, Any]:
        """Return the one successful temporary hotspot batch that may be reused in a validation."""
        rows = self.conn.execute(
            "SELECT collection_run_id, status, item_count FROM trendradar_collection_run "
            "WHERE discovery_run_id=? ORDER BY started_at DESC, collection_run_id ASC",
            (discovery_run_id,),
        ).fetchall()
        if len(rows) != 1:
            raise StateTransitionError("a reusable hotspot batch must have exactly one recorded collection")
        row = rows[0]
        if str(row["status"]) != "completed" or int(row["item_count"] or 0) < 1:
            raise StateTransitionError("only a successful non-empty hotspot collection can be reused")
        observation_count = int(self.conn.execute(
            "SELECT COUNT(*) FROM trendradar_hotspot_observation WHERE collection_run_id=?",
            (str(row["collection_run_id"]),),
        ).fetchone()[0])
        if observation_count < 1:
            raise StateTransitionError("the successful hotspot batch has already been cleared and cannot be reused")
        return {
            "discovery_run_id": discovery_run_id,
            "collection_run_id": str(row["collection_run_id"]),
            "item_count": int(row["item_count"]),
            "observation_count": observation_count,
        }



    def load_real_discovery_sources(
        self,
        *,
        domain_label: str,
        daily_since: str,
        per_source_limit: int,
        hotspot_discovery_run_id: str | None = None,
    ) -> list[dict[str, Any]]:
        """Read only qualified, already-recorded formal source facts; never collect or invent content."""
        if self.discovery_source_readiness(
            domain_label=domain_label,
            daily_since=daily_since,
            hotspot_discovery_run_id=hotspot_discovery_run_id,
        )["status"] != "ready":
            return []
        result: list[dict[str, Any]] = []
        tables = {row["name"] for row in self.conn.execute("SELECT name FROM sqlite_master WHERE type='table'").fetchall()}
        competitor_ready = {"competitor_accounts", "competitor_videos", "hits"}.issubset(tables)
        hotspot_scope = ""
        hotspot_params = [daily_since]
        if hotspot_discovery_run_id is not None:
            hotspot_scope = " AND run.discovery_run_id=? "
            hotspot_params.append(hotspot_discovery_run_id)
        all_hotspot_rows = self.conn.execute(
            "SELECT observation.* FROM trendradar_hotspot_observation observation "
            "JOIN trendradar_collection_run run ON run.collection_run_id=observation.collection_run_id "
            "WHERE observation.observed_at>=? AND run.status IN ('completed', 'completed_with_failures') "
            + hotspot_scope +
            "ORDER BY observation.observed_at DESC, observation.observation_id ASC",
            tuple(hotspot_params),
        ).fetchall()
        result.extend(
            _build_hotspot_event_cluster_sources(
                list(all_hotspot_rows),
                per_source_limit=per_source_limit,
            )
        )
        daily_rows = self.conn.execute(
            "SELECT video.video_id, video.title, video.url, video.publish_time, video.last_checked_at, video.raw_json, account.account_name "
            "FROM competitor_videos video JOIN competitor_accounts account ON account.account_id=video.account_id "
            "WHERE account.domain_label=? AND account.registration_status='active' AND COALESCE(account.source_config_ref, '')<>'' "
            "AND video.publish_time>=? AND COALESCE(video.title, '')<>'' AND COALESCE(video.url, '')<>'' "
            "AND COALESCE(video.raw_archive_ref, '')<>'' AND video.excluded_reason IS NULL "
            "ORDER BY video.publish_time DESC, video.video_id ASC LIMIT ?",
            (domain_label, daily_since, per_source_limit),
        ).fetchall() if competitor_ready else []
        for row in daily_rows:
            raw_hash = _hash(json.loads(row["raw_json"]))
            result.append({"source_type": "daily_competitor_content", "source_object_id": row["video_id"], "source_object_version": row["last_checked_at"], "source_time": row["publish_time"], "payload": {"source_id": row["video_id"], "title": row["title"], "url": row["url"], "account_name": row["account_name"], "source_time": row["publish_time"], "formal_source": {"table": "competitor_videos", "object_id": row["video_id"], "object_version": row["last_checked_at"], "raw_metadata_hash": raw_hash}}})
        historical_rows = self.conn.execute(
            "SELECT hit.hit_id, hit.title, hit.url, hit.publish_time, hit.promoted_at, hit.hit_channel, hit.judgment_confidence, "
            "video.raw_json, account.account_name FROM hits hit JOIN competitor_videos video ON video.video_id=hit.video_id "
            "JOIN competitor_accounts account ON account.account_id=hit.account_id WHERE account.domain_label=? "
            "AND account.registration_status='active' AND COALESCE(account.source_config_ref, '')<>'' "
            "AND hit.judgment_confidence IN ('rough', 'formal') AND COALESCE(hit.title, '')<>'' AND COALESCE(hit.url, '')<>'' "
            "AND COALESCE(video.raw_archive_ref, '')<>'' AND video.excluded_reason IS NULL "
            "ORDER BY hit.promoted_at DESC, hit.hit_id ASC LIMIT ?",
            (domain_label, per_source_limit),
        ).fetchall() if competitor_ready else []
        for row in historical_rows:
            raw_hash = _hash(json.loads(row["raw_json"]))
            result.append({"source_type": "historical_high_signal", "source_object_id": row["hit_id"], "source_object_version": row["promoted_at"], "source_time": row["publish_time"], "payload": {"source_id": row["hit_id"], "title": row["title"], "url": row["url"], "account_name": row["account_name"], "source_time": row["publish_time"], "signal_basis": row["hit_channel"], "signal_confidence": row["judgment_confidence"], "formal_source": {"table": "hits", "object_id": row["hit_id"], "object_version": row["promoted_at"], "raw_metadata_hash": raw_hash}}})
        tag_rows = self.conn.execute(
            "SELECT video.*, tag.tag FROM discovered_external_videos video "
            "JOIN domain_search_tags tag ON tag.tag_id=video.tag_id WHERE video.domain_label=? "
            "AND tag.status='active' AND COALESCE(video.title, '')<>'' AND COALESCE(video.url, '')<>'' "
            "ORDER BY video.discovered_at DESC, video.discovered_video_id ASC LIMIT ?",
            (domain_label, per_source_limit),
        ).fetchall()
        for row in tag_rows:
            raw_hash = _hash(json.loads(row["raw_json"]))
            result.append({
                "source_type": "tag_discovery",
                "source_object_id": row["discovered_video_id"],
                "source_object_version": row["discovered_at"],
                "source_time": row["discovered_at"],
                "payload": {
                    "source_id": row["discovered_video_id"],
                    "title": row["title"],
                    "url": row["url"],
                    "platform": row["platform"],
                    "platform_item_id": row["platform_item_id"],
                    "account_platform_id": row["account_platform_id"],
                    "account_name": row["account_handle"] or row["account_platform_id"],
                    "discovery_tag": row["tag"],
                    "source_time": row["discovered_at"],
                    "formal_source": {
                        "table": "discovered_external_videos",
                        "object_id": row["discovered_video_id"],
                        "object_version": row["discovered_at"],
                        "raw_metadata_hash": raw_hash,
                    },
                },
            })
        expansion_rows = self.conn.execute(
            "SELECT * FROM stage1_question_expansion_source WHERE domain_label=? "
            "AND validation_outcome='supported' AND data_identity=? ORDER BY validated_at DESC, expansion_id ASC LIMIT ?",
            (domain_label, self.data_identity, per_source_limit),
        ).fetchall()
        for row in expansion_rows:
            payload = json.loads(row["payload_json"])
            result.append({
                "source_type": "question_expansion",
                "source_object_id": row["expansion_id"],
                "source_object_version": row["integrity_hash"],
                "source_time": row["validated_at"],
                "payload": {
                    **payload,
                    "source_id": row["expansion_id"],
                    "url": "",
                    "account_name": "已完成拓展验证",
                    "source_time": row["validated_at"],
                    "formal_source": {
                        "table": "stage1_question_expansion_source",
                        "object_id": row["expansion_id"],
                        "object_version": row["integrity_hash"],
                        "raw_metadata_hash": row["integrity_hash"],
                    },
                },
            })
        direction_rows = self.conn.execute(
            "SELECT * FROM stage1_saved_user_direction_source WHERE domain_label=? "
            "AND status='active' AND data_identity=? ORDER BY saved_at DESC, direction_id ASC LIMIT ?",
            (domain_label, self.data_identity, per_source_limit),
        ).fetchall()
        for row in direction_rows:
            payload = json.loads(row["payload_json"])
            result.append({
                "source_type": "saved_user_direction",
                "source_object_id": row["direction_id"],
                "source_object_version": row["integrity_hash"],
                "source_time": row["saved_at"],
                "payload": {
                    **payload,
                    "source_id": row["direction_id"],
                    "url": "",
                    "account_name": "用户保存方向",
                    "source_time": row["saved_at"],
                    "formal_source": {
                        "table": "stage1_saved_user_direction_source",
                        "object_id": row["direction_id"],
                        "object_version": row["integrity_hash"],
                        "raw_metadata_hash": row["integrity_hash"],
                    },
                },
            })
        return result

    def discovery_source_seen(self, *, source_type: str, source_object_id: str, source_object_version: str) -> bool:
        """Only a user-selected production candidate consumes a source version.

        Validation, interrupted work and unresolved candidate attempts are evidence,
        not an editorial decision.  They must never make the next daily run pretend
        that a source has already been processed.
        """
        if self.data_identity != "production":
            row = self.conn.execute(
                "SELECT 1 FROM stage1b_source_version WHERE source_type=? AND source_object_id=? "
                "AND source_object_version=? AND data_identity=? LIMIT 1",
                (source_type, source_object_id, source_object_version, self.data_identity),
            ).fetchone()
            return row is not None
        row = self.conn.execute(
            "SELECT 1 FROM stage1b_source_version source "
            "JOIN stage1b_discovery_run run ON run.run_id=source.run_id "
            "JOIN stage1b_run_execution_context context ON context.run_id=run.run_id "
            "JOIN stage1b_candidate_version candidate ON candidate.source_version_id=source.source_version_id "
            "JOIN stage1b_candidate_decision decision ON decision.candidate_version_id=candidate.candidate_version_id "
            "WHERE source.source_type=? AND source.source_object_id=? AND source.source_object_version=? "
            "AND source.data_identity=? AND context.execution_mode='production_daily' "
            "AND context.lifecycle_status IN ('completed', 'completed_with_failures') "
            "AND decision.decision='selected' LIMIT 1",
            (source_type, source_object_id, source_object_version, self.data_identity),
        ).fetchone()
        return row is not None

    def formal_topic_title_seen(self, *, domain_label: str, normalized_title: str) -> bool:
        tables = {row["name"] for row in self.conn.execute("SELECT name FROM sqlite_master WHERE type='table'").fetchall()}
        if not {"stage1a_artifact_payload", "stage0_content_node_version"}.issubset(tables):
            return False
        rows = self.conn.execute(
            "SELECT artifact.payload_json FROM stage1a_artifact_payload artifact JOIN stage0_content_node_version version ON version.version_id=artifact.version_id WHERE artifact.artifact_kind='formal_topic' AND artifact.data_identity=?",
            (self.data_identity,),
        ).fetchall()
        return any(json.loads(row["payload_json"]).get("domain") == domain_label and str(json.loads(row["payload_json"]).get("title", "")).casefold() == normalized_title for row in rows)

    def domain_formal_topic_count(self, *, domain_label: str, current_date: str) -> int:
        tables = {row["name"] for row in self.conn.execute("SELECT name FROM sqlite_master WHERE type='table'").fetchall()}
        if not {"stage1a_artifact_payload", "stage0_content_node_version"}.issubset(tables):
            return 0
        rows = self.conn.execute(
            "SELECT artifact.payload_json FROM stage1a_artifact_payload artifact JOIN stage0_content_node_version version ON version.version_id=artifact.version_id WHERE artifact.artifact_kind='formal_topic' AND artifact.data_identity=? AND substr(version.created_at, 1, 10)=?",
            (self.data_identity, current_date),
        ).fetchall()
        return sum(1 for row in rows if json.loads(row["payload_json"]).get("domain") == domain_label)

    def get_discovery_candidate(self, candidate_version_id: str) -> dict[str, Any]:
        row = self._discovery_candidate(candidate_version_id)
        return {key: row[key] for key in row.keys()} | {"payload": json.loads(row["payload_json"])}

    def get_artifact_payload(self, version_id: str) -> dict[str, Any]:
        version = self._version(version_id)
        row = self.conn.execute(
            "SELECT artifact_kind, payload_json, integrity_hash, created_at FROM stage1a_artifact_payload WHERE version_id=? AND data_identity=?",
            (version_id, self.data_identity),
        ).fetchone()
        if row is None:
            row = self.conn.execute(
                "SELECT artifact_kind, payload_json, integrity_hash, created_at FROM stage0_content_artifact_payload WHERE version_id=? AND data_identity=?",
                (version_id, self.data_identity),
            ).fetchone()
        if row is None:
            raise StateTransitionError("artifact payload does not exist for this version")
        return {
            "version_id": version_id,
            "task_id": version["task_id"],
            "node": version["node"],
            "artifact_kind": row["artifact_kind"],
            "payload": json.loads(row["payload_json"]),
            "integrity_hash": row["integrity_hash"],
            "created_at": row["created_at"],
        }

    def _insert_artifact_payload(self, version_id: str, artifact_kind: str, payload: dict[str, Any]) -> None:
        table = "stage1a_artifact_payload" if artifact_kind in {"formal_topic", "research_plan"} else "stage0_content_artifact_payload"
        self.conn.execute(
            f"INSERT INTO {table} VALUES (?, ?, ?, ?, ?, ?)",
            (version_id, artifact_kind, _canonical(payload), _hash(payload), self.data_identity, _now()),
        )

    def _approved_upstream(self, task: sqlite3.Row, node: str, upstream_version_id: str | None) -> str:
        expected_node = UPSTREAM_NODE[node]
        if not upstream_version_id:
            raise StateTransitionError("exact approved upstream version is required")
        upstream = self._version(upstream_version_id)
        if upstream["task_id"] != task["task_id"] or upstream["node"] != expected_node:
            raise StateTransitionError("upstream version is not the adjacent required node")
        approved = self.conn.execute(
            "SELECT 1 FROM stage0_content_decision WHERE version_id=? AND decision='approved'", (upstream_version_id,)
        ).fetchone()
        if approved is None:
            raise StateTransitionError("upstream version is not approved")
        return upstream_version_id

    @staticmethod
    def _assert_current_node(task: sqlite3.Row, node: str, status: str, version_id: str | None = None) -> None:
        if task["current_node"] != node or task["current_status"] != status:
            raise StateTransitionError(f"task is at {task['current_node']}/{task['current_status']}, not {node}/{status}")
        if version_id is not None and task["current_version_id"] != version_id:
            raise StaleResultError("node version is no longer current")

    def _set_task(self, task_id: str, *, node: str, version_id: str, status: str, revision: int) -> None:
        self.conn.execute(
            "UPDATE stage0_content_task SET current_node=?, current_version_id=?, current_status=?, task_revision=? WHERE task_id=?",
            (node, version_id, status, revision, task_id),
        )

    def _decision(self, task_id: str, node: str, version_id: str, decision: str, actor: str, actor_kind: str, reason: str) -> None:
        self.conn.execute(
            "INSERT INTO stage0_content_decision VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (_id("decision"), task_id, node, version_id, decision, actor, actor_kind, reason, _now(), self.data_identity),
        )

    def _replay(self, scope: str, key: str, request: dict[str, Any]) -> dict[str, Any] | None:
        if not key:
            raise StateTransitionError("idempotency_key is required")
        row = self.conn.execute("SELECT request_hash, result_json FROM stage0_command_receipt WHERE command_scope=? AND idempotency_key=?", (scope, key)).fetchone()
        if row is None:
            return None
        if row["request_hash"] != _hash(request):
            raise StateTransitionError("idempotency key was reused with a different request")
        value = json.loads(row["result_json"])
        if not isinstance(value, dict):
            raise StateTransitionError("stored command result is not an object")
        return {str(key): item for key, item in value.items()}

    def _receipt(self, scope: str, key: str, request: dict[str, Any], result: dict[str, Any]) -> None:
        self.conn.execute("INSERT INTO stage0_command_receipt VALUES (?, ?, ?, ?, ?)", (scope, key, _hash(request), _canonical(result), _now()))

    def _audit(self, task_id: str | None, action: str, payload: dict[str, Any]) -> None:
        self.conn.execute("INSERT INTO stage0_audit_event VALUES (?, ?, ?, ?, ?, ?)", (_id("audit"), task_id, action, _canonical(payload), self.data_identity, _now()))
