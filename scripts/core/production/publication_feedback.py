"""One formal entry for external publication registration and P0-P7 feedback.

The system does not create or publish videos. This module records facts the
user confirms after an external publication, stores authorized observations,
and prepares a P7 feedback candidate. It never writes experience or changes a
long-term rule automatically.
"""

from __future__ import annotations

import json
import sqlite3
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Mapping
from urllib.parse import urlparse
from uuid import uuid4


SCHEMA_PATH = Path(__file__).with_name("publication_feedback_schema.sqlite.sql")
POINT_CODES = tuple(f"P{i}" for i in range(8))
OBSERVATION_STATUSES = {"recorded", "missing"}


class PublicationFeedbackError(ValueError):
    """Raised when a publication feedback action violates the formal rules."""


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _text(value: Any, field: str) -> str:
    result = str(value or "").strip()
    if not result:
        raise PublicationFeedbackError(f"{field} is required")
    return result


def _json_object(value: Any, field: str) -> str:
    if value is None:
        value = {}
    if not isinstance(value, dict):
        raise PublicationFeedbackError(f"{field} must be an object")
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def _same_payload(row: sqlite3.Row, expected: dict[str, Any]) -> bool:
    return all(str(row[key] or "") == str(value or "") for key, value in expected.items())


def _validation_usage_records(
    conn: sqlite3.Connection, *, task_id: str, data_identity: str
) -> list[dict[str, Any]]:
    """Read validation-candidate usage from the existing Core audit ledger."""

    table = conn.execute(
        "SELECT 1 FROM sqlite_master WHERE type='table' AND name='stage0_audit_event'"
    ).fetchone()
    if table is None:
        return []
    rows = conn.execute(
        "SELECT payload_json FROM stage0_audit_event "
        "WHERE task_id=? AND action='validation_candidate_used' AND data_identity=? "
        "ORDER BY created_at, audit_id",
        (task_id, data_identity),
    ).fetchall()
    records: list[dict[str, Any]] = []
    for row in rows:
        try:
            payload = json.loads(str(row["payload_json"]))
        except (TypeError, ValueError, json.JSONDecodeError):
            continue
        if isinstance(payload, dict) and str(payload.get("experience_candidate_id") or "").strip():
            records.append(payload)
    return records


def _normalize_validation_results(
    conn: sqlite3.Connection,
    *,
    task_id: str,
    feedback_candidate: Mapping[str, Any] | None,
    data_identity: str,
) -> dict[str, Any]:
    usage_records = _validation_usage_records(
        conn, task_id=task_id, data_identity=data_identity
    )
    payload = dict(feedback_candidate or {})
    raw_results = payload.get("validation_results")
    if not usage_records:
        if raw_results is not None:
            raise PublicationFeedbackError(
                "validation results require a validation candidate used by this task"
            )
        return payload
    if not isinstance(raw_results, list):
        raise PublicationFeedbackError(
            "P7 review requires structured validation results for every used candidate"
        )
    by_candidate: dict[str, list[str]] = {}
    for record in usage_records:
        candidate_id = str(record.get("experience_candidate_id") or "").strip()
        usage_id = str(record.get("validation_usage_id") or "").strip()
        if candidate_id and usage_id:
            by_candidate.setdefault(candidate_id, []).append(usage_id)
    if not by_candidate:
        raise PublicationFeedbackError("validation usage records are missing candidate identity")
    if len(raw_results) != len(by_candidate):
        raise PublicationFeedbackError(
            "P7 validation results must contain exactly one result for each used candidate"
        )
    normalized: list[dict[str, Any]] = []
    seen: set[str] = set()
    for item in raw_results:
        if not isinstance(item, Mapping):
            raise PublicationFeedbackError("each P7 validation result must be an object")
        candidate_id = str(item.get("experience_candidate_id") or "").strip()
        result = str(item.get("result") or "").strip()
        performance_summary = str(item.get("performance_summary") or "").strip()
        review_basis = str(item.get("review_basis") or "").strip()
        if candidate_id not in by_candidate or candidate_id in seen:
            raise PublicationFeedbackError("P7 validation result references an unexpected candidate")
        if result not in {"supports", "contradicts", "inconclusive"}:
            raise PublicationFeedbackError(
                "P7 validation result must be supports, contradicts or inconclusive"
            )
        if not performance_summary or not review_basis:
            raise PublicationFeedbackError(
                "P7 validation result requires performance and review basis"
            )
        normalized.append({
            **dict(item),
            "experience_candidate_id": candidate_id,
            "result": result,
            "performance_summary": performance_summary,
            "review_basis": review_basis,
            "validation_usage_ids": sorted(by_candidate[candidate_id]),
        })
        seen.add(candidate_id)
    payload["validation_results"] = normalized
    return payload


def install_schema(conn: sqlite3.Connection) -> None:
    conn.executescript(SCHEMA_PATH.read_text(encoding="utf-8"))


def _require_owned_account(
    conn: sqlite3.Connection,
    *,
    account_id: str,
    domain_label: str,
    data_identity: str,
) -> None:
    row = conn.execute(
        """
        SELECT account_role, domain_label, status
          FROM stage0_content_account
         WHERE content_account_id=? AND data_identity=?
        """,
        (account_id, data_identity),
    ).fetchone()
    if row is None:
        raise PublicationFeedbackError("publication account does not exist in this data identity")
    if row["account_role"] != "owned":
        raise PublicationFeedbackError("publication registration requires a self-owned account")
    if row["domain_label"] != domain_label:
        raise PublicationFeedbackError("publication account and domain do not match")
    if row["status"] in {"archived", "disabled"}:
        raise PublicationFeedbackError("publication account is not active")


def _require_approved_audio(
    conn: sqlite3.Connection,
    *,
    task_id: str,
    audio_delivery_id: str,
    approved_content_version_id: str,
    data_identity: str,
) -> None:
    row = conn.execute(
        """
        SELECT task_id, approved_content_version_id, status
          FROM stage0_audio_delivery
         WHERE audio_delivery_id=? AND data_identity=?
        """,
        (audio_delivery_id, data_identity),
    ).fetchone()
    if row is None:
        raise PublicationFeedbackError("approved audio delivery does not exist in this data identity")
    if row["task_id"] != task_id or row["approved_content_version_id"] != approved_content_version_id:
        raise PublicationFeedbackError("publication must reference the exact approved content and audio task")
    if row["status"] != "delivered":
        raise PublicationFeedbackError("publication registration requires approved audio")


def register_publication(
    conn: sqlite3.Connection,
    *,
    publication_id: str,
    content_account_id: str,
    domain_label: str,
    task_id: str,
    audio_delivery_id: str,
    approved_content_version_id: str,
    platform: str,
    external_video_url: str,
    published_at: str,
    actual_content_status: str,
    actual_content_note: str,
    confirmed_by: str,
    data_identity: str,
    created_by: str,
) -> dict[str, Any]:
    publication_id = _text(publication_id, "publication_id")
    account_id = _text(content_account_id, "content_account_id")
    domain_label = _text(domain_label, "domain_label")
    task_id = _text(task_id, "task_id")
    audio_delivery_id = _text(audio_delivery_id, "audio_delivery_id")
    approved_content_version_id = _text(approved_content_version_id, "approved_content_version_id")
    platform = _text(platform, "platform")
    external_video_url = _text(external_video_url, "external_video_url")
    published_at = _text(published_at, "published_at")
    confirmed_by = _text(confirmed_by, "confirmed_by")
    data_identity = _text(data_identity, "data_identity")
    created_by = _text(created_by, "created_by")
    if urlparse(external_video_url).scheme not in {"http", "https"}:
        raise PublicationFeedbackError("external_video_url must be an http or https URL")
    if actual_content_status not in {"same_as_approved", "different_from_approved"}:
        raise PublicationFeedbackError("actual_content_status must explicitly say same_as_approved or different_from_approved")
    if actual_content_status == "different_from_approved" and not str(actual_content_note or "").strip():
        raise PublicationFeedbackError("a changed publication version requires a user note")

    _require_owned_account(
        conn, account_id=account_id, domain_label=domain_label, data_identity=data_identity,
    )
    _require_approved_audio(
        conn,
        task_id=task_id,
        audio_delivery_id=audio_delivery_id,
        approved_content_version_id=approved_content_version_id,
        data_identity=data_identity,
    )
    existing = conn.execute(
        "SELECT * FROM stage0_publication_registration WHERE publication_id=? AND data_identity=?",
        (publication_id, data_identity),
    ).fetchone()
    expected = {
        "content_account_id": account_id,
        "domain_label": domain_label,
        "task_id": task_id,
        "audio_delivery_id": audio_delivery_id,
        "approved_content_version_id": approved_content_version_id,
        "platform": platform,
        "external_video_url": external_video_url,
        "published_at": published_at,
        "actual_content_status": actual_content_status,
        "actual_content_note": str(actual_content_note or "").strip(),
        "confirmed_by": confirmed_by,
        "created_by": created_by,
    }
    if existing is not None:
        if _same_payload(existing, expected):
            return {"publication_id": publication_id, "status": existing["status"], "idempotent": True}
        raise PublicationFeedbackError("publication_id already contains different confirmed facts")
    try:
        conn.execute(
            """
            INSERT INTO stage0_publication_registration(
                publication_id, content_account_id, domain_label, task_id,
                audio_delivery_id, approved_content_version_id, platform,
                external_video_url, published_at, actual_content_status,
                actual_content_note, confirmed_by, confirmed_at, status,
                data_identity, created_by, created_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 'registered', ?, ?, ?)
            """,
            (
                publication_id, account_id, domain_label, task_id,
                audio_delivery_id, approved_content_version_id, platform,
                external_video_url, published_at, actual_content_status,
                str(actual_content_note or "").strip(), confirmed_by, _now(),
                data_identity, created_by, _now(),
            ),
        )
        conn.commit()
    except sqlite3.IntegrityError as exc:
        conn.rollback()
        raise PublicationFeedbackError("this external publication is already registered or conflicts with an existing record") from exc
    return {"publication_id": publication_id, "status": "registered", "idempotent": False}


def record_observation(
    conn: sqlite3.Connection,
    *,
    observation_id: str,
    publication_id: str,
    point_code: str,
    observation_status: str,
    metrics: dict[str, Any] | None,
    missing_reason: str,
    source_ref: str,
    observed_at: str,
    recorded_by: str,
    data_identity: str,
) -> dict[str, Any]:
    observation_id = _text(observation_id, "observation_id")
    publication_id = _text(publication_id, "publication_id")
    point_code = _text(point_code, "point_code")
    recorded_by = _text(recorded_by, "recorded_by")
    data_identity = _text(data_identity, "data_identity")
    observed_at = _text(observed_at, "observed_at")
    if point_code not in POINT_CODES:
        raise PublicationFeedbackError("point_code must be one of P0 through P7")
    if observation_status not in OBSERVATION_STATUSES:
        raise PublicationFeedbackError("observation_status must be recorded or missing")
    missing_reason = str(missing_reason or "").strip()
    if observation_status == "missing" and not missing_reason:
        raise PublicationFeedbackError("missing observations require a reason")
    metrics_json = _json_object(metrics, "metrics")
    publication = conn.execute(
        "SELECT publication_id FROM stage0_publication_registration WHERE publication_id=? AND data_identity=?",
        (publication_id, data_identity),
    ).fetchone()
    if publication is None:
        raise PublicationFeedbackError("publication does not exist in this data identity")
    existing = conn.execute(
        """
        SELECT * FROM stage0_publication_observation
         WHERE publication_id=? AND point_code=? AND data_identity=?
        """,
        (publication_id, point_code, data_identity),
    ).fetchone()
    expected = {
        "observation_status": observation_status,
        "metrics_json": metrics_json,
        "missing_reason": missing_reason,
        "source_ref": str(source_ref or "").strip(),
        "observed_at": observed_at,
        "recorded_by": recorded_by,
    }
    if existing is not None:
        if _same_payload(existing, expected):
            return {"observation_id": existing["observation_id"], "idempotent": True}
        raise PublicationFeedbackError("this P-point already has a different observation")
    try:
        conn.execute(
            """
            INSERT INTO stage0_publication_observation(
                observation_id, publication_id, point_code, observation_status,
                metrics_json, missing_reason, source_ref, observed_at,
                recorded_by, data_identity, created_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                observation_id, publication_id, point_code, observation_status,
                metrics_json, missing_reason, str(source_ref or "").strip(),
                observed_at, recorded_by, data_identity, _now(),
            ),
        )
        conn.commit()
    except sqlite3.IntegrityError as exc:
        conn.rollback()
        raise PublicationFeedbackError("this P-point is already recorded or conflicts with another record") from exc
    return {"observation_id": observation_id, "idempotent": False}


def prepare_p7_review(
    conn: sqlite3.Connection,
    *,
    review_id: str,
    publication_id: str,
    selection_assessment: str,
    narrative_assessment: str,
    material_assessment: str,
    external_conditions_assessment: str,
    feedback_candidate: dict[str, Any] | None,
    created_by: str,
    data_identity: str,
) -> dict[str, Any]:
    review_id = _text(review_id, "review_id")
    publication_id = _text(publication_id, "publication_id")
    created_by = _text(created_by, "created_by")
    data_identity = _text(data_identity, "data_identity")
    assessments = {
        "selection_assessment": _text(selection_assessment, "selection_assessment"),
        "narrative_assessment": _text(narrative_assessment, "narrative_assessment"),
        "material_assessment": _text(material_assessment, "material_assessment"),
        "external_conditions_assessment": _text(external_conditions_assessment, "external_conditions_assessment"),
    }
    publication = conn.execute(
        "SELECT publication_id, task_id FROM stage0_publication_registration WHERE publication_id=? AND data_identity=?",
        (publication_id, data_identity),
    ).fetchone()
    if publication is None:
        raise PublicationFeedbackError("publication does not exist in this data identity")
    rows = conn.execute(
        "SELECT point_code FROM stage0_publication_observation WHERE publication_id=? AND data_identity=?",
        (publication_id, data_identity),
    ).fetchall()
    observed = {row["point_code"] for row in rows}
    missing_points = [point for point in POINT_CODES if point not in observed]
    if missing_points:
        raise PublicationFeedbackError(f"P7 review requires every observation point, missing: {', '.join(missing_points)}")
    feedback_payload = _normalize_validation_results(
        conn,
        task_id=str(publication["task_id"]),
        feedback_candidate=feedback_candidate,
        data_identity=data_identity,
    )
    candidate_json = _json_object(feedback_payload, "feedback_candidate")
    existing = conn.execute(
        "SELECT * FROM stage0_publication_p7_review WHERE publication_id=? AND data_identity=?",
        (publication_id, data_identity),
    ).fetchone()
    expected = {**assessments, "feedback_candidate_json": candidate_json, "created_by": created_by}
    if existing is not None:
        if existing["status"] != "awaiting_user_confirmation":
            raise PublicationFeedbackError("a decided P7 review cannot be overwritten")
        if _same_payload(existing, expected):
            return {"review_id": existing["review_id"], "status": existing["status"], "idempotent": True}
        raise PublicationFeedbackError("an awaiting P7 review already exists and cannot be overwritten")
    try:
        conn.execute(
            """
            INSERT INTO stage0_publication_p7_review(
                review_id, publication_id, selection_assessment,
                narrative_assessment, material_assessment,
                external_conditions_assessment, feedback_candidate_json,
                status, data_identity, created_by, created_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, 'awaiting_user_confirmation', ?, ?, ?)
            """,
            (
                review_id, publication_id, assessments["selection_assessment"],
                assessments["narrative_assessment"], assessments["material_assessment"],
                assessments["external_conditions_assessment"], candidate_json,
                data_identity, created_by, _now(),
            ),
        )
        conn.commit()
    except sqlite3.IntegrityError as exc:
        conn.rollback()
        raise PublicationFeedbackError("this publication already has a P7 review") from exc
    return {"review_id": review_id, "status": "awaiting_user_confirmation", "idempotent": False}


def decide_p7_review(
    conn: sqlite3.Connection,
    *,
    review_id: str,
    decision: str,
    actor: str,
    reason: str,
    data_identity: str,
) -> dict[str, Any]:
    review_id = _text(review_id, "review_id")
    actor = _text(actor, "actor")
    reason = _text(reason, "reason")
    data_identity = _text(data_identity, "data_identity")
    if decision not in {"confirmed", "rejected"}:
        raise PublicationFeedbackError("P7 review decision must be confirmed or rejected")
    row = conn.execute(
        "SELECT status FROM stage0_publication_p7_review WHERE review_id=? AND data_identity=?",
        (review_id, data_identity),
    ).fetchone()
    if row is None:
        raise PublicationFeedbackError("P7 review does not exist in this data identity")
    if row["status"] != "awaiting_user_confirmation":
        raise PublicationFeedbackError("P7 review has already been decided")
    conn.execute(
        """
        UPDATE stage0_publication_p7_review
           SET status=?, decision_by=?, decision_reason=?, decided_at=?
         WHERE review_id=? AND data_identity=?
        """,
        (decision, actor, reason, _now(), review_id, data_identity),
    )
    conn.commit()
    return {
        "review_id": review_id,
        "status": decision,
        "experience_write": "not_automatic",
        "rule_change": "not_automatic",
    }
