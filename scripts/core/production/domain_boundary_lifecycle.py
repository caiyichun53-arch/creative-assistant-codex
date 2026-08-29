"""Cold-start domain production-boundary lifecycle.

The module is attached to the existing Stage 0 Core at import time.  It owns
only the 3B-2 candidate/review/freeze lifecycle; it does not alter content
types, tags, cold-start completion, or candidate scoring.
"""

from __future__ import annotations

import json
from collections.abc import Mapping
from pathlib import Path
from typing import Any, Callable

from scripts.core.business_data.domain_boundaries import (
    evaluate_production_boundary,
    freeze_production_boundary_registry,
    get_production_boundary_registry,
    require_frozen_production_boundary,
)
_BANNED_FORM_TERMS = (
    "内容类型", "表达形式", "内容路线", "故事式", "盘点式",
    "对比式", "问答式", "时间线式", "采访式",
)


def _observation_snapshot(self: Any, *, cold_start_id: str) -> dict[str, Any]:
    cold_start = self.conn.execute(
        "SELECT cold_start_id, domain_label, status FROM stage0_cold_start "
        "WHERE cold_start_id=? AND data_identity=?",
        (cold_start_id, self.data_identity),
    ).fetchone()
    if cold_start is None:
        raise self.StateTransitionError("domain-boundary observations require the current data identity")
    if str(cold_start["status"]) != "running":
        raise self.StateTransitionError("only running cold starts can produce boundary candidates")
    rows = self.conn.execute(
        "SELECT item.registration_id, item.item_ref, item.artifact_json, "
        "registration.competitor_account_id, account.display_name "
        "FROM stage0_competitor_registration_item item "
        "JOIN stage0_competitor_registration registration "
        "ON registration.registration_id=item.registration_id "
        "AND registration.data_identity=item.data_identity "
        "JOIN stage0_content_account account "
        "ON account.content_account_id=registration.competitor_account_id "
        "AND account.data_identity=registration.data_identity "
        "AND account.account_role='competitor' "
        "WHERE registration.cold_start_id=? AND item.data_identity=? "
        "AND item.step_name='breakdown' AND item.status='completed' "
        "ORDER BY registration.competitor_account_id, item.item_ref",
        (cold_start_id, self.data_identity),
    ).fetchall()
    observations: list[dict[str, Any]] = []
    ignored: list[dict[str, Any]] = []
    for row in rows:
        item_ref = str(row["item_ref"] or "").strip()
        try:
            artifact = json.loads(str(row["artifact_json"] or "{}"))
        except (TypeError, ValueError, json.JSONDecodeError):
            ignored.append({"source_id": item_ref, "reason": "breakdown_artifact_is_not_valid_json"})
            continue
        breakdown = artifact.get("deep_breakdown") if isinstance(artifact, dict) else None
        breakdown = breakdown if isinstance(breakdown, dict) else artifact
        if not isinstance(breakdown, dict):
            ignored.append({"source_id": item_ref, "reason": "breakdown_artifact_has_no_deep_breakdown_object"})
            continue
        boundary_observation = str(breakdown.get("boundary_observation") or "").strip()
        if not boundary_observation:
            ignored.append({"source_id": item_ref, "reason": "boundary_observation_is_empty"})
            continue

        observations.append({
            "source_id": item_ref,
            "boundary_observation": boundary_observation,
            "source_ref": {
                "cold_start_id": cold_start_id,
                "registration_id": str(row["registration_id"]),
                "competitor_account_id": str(row["competitor_account_id"]),
                "account_display_name": str(row["display_name"] or ""),
                "source_id": item_ref,
                "item_ref": item_ref,
                "data_identity": self.data_identity,
            },
        })
    return {
        "cold_start_id": cold_start_id,
        "domain_label": str(cold_start["domain_label"]),
        "cold_start_status": str(cold_start["status"]),
        "valid_observations": observations,
        "ignored_observations": ignored,
        "valid_count": len(observations),
        "ignored_count": len(ignored),
    }


def _default_isolated_proposal(snapshot: dict[str, Any]) -> dict[str, Any]:
    refs = [str(item["source_id"]) for item in snapshot.get("valid_observations", [])]
    return {
        "in_boundary_principles": [{
            "boundary_id": "in_sample_supported_problem_nature",
            "principle": "围绕当前样本明确呈现的对象与其具体问题、关系、变化或影响来生产，内容必须能回到这些问题性质本身完成交付。",
            "rationale": "当前运行样本的拆解分析共同记录了具体对象与问题推进，支持抽象出问题性质边界。",
            "evidence_refs": refs,
        }],
        "out_boundary_principles": [{
            "boundary_id": "out_surface_association_only",
            "principle": "仅因为对象名称、标签或领域表面关联而进入，但没有形成当前样本所体现的问题关系或具体影响的问题，不属于本领域生产范围。",
            "rationale": "当前样本证据都围绕具体问题推进，不能把单纯实体关联当作生产依据。",
            "evidence_refs": refs,
        }],
        "unknown_topic_rule": {
            "rule": "未来新对象或新题材不按是否在样本出现过判断，而按其问题性质是否符合已冻结的边界原则判断。",
            "uncertain_action": "原则无法判断时先进入用户确认，不自行放行或拒绝。",
        },
    }


def _proposal_prompt(snapshot: dict[str, Any]) -> str:
    return (
        "你只负责从当前冷启动样本中提出候选领域生产边界，只输出JSON对象。"
        "边界必须是可判断未来新题材的问题性质原则，不得列举题材、标题、对象或关键词白名单。"
        "不得使用内容类型、表达形式、内容路线或标签作为边界。"
        "每条边界原则必须引用至少一个输入source_id。"
        "必须返回in_boundary_principles、out_boundary_principles、unknown_topic_rule三个键。"
        "每条原则包含boundary_id、principle、rationale、evidence_refs。"
        "unknown_topic_rule包含rule和uncertain_action。输入样本如下："
        + self_canonical(snapshot)
    )


def self_canonical(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


_DOMAIN_BOUNDARY_SKILL_VERSION = "domain_boundary_proposal.v1"
_DOMAIN_BOUNDARY_OUTPUT_SCHEMA = {
    "type": "object",
    "required": ["in_boundary_principles", "out_boundary_principles", "unknown_topic_rule"],
    "additionalProperties": False,
}


def _domain_boundary_external_task(
    self: Any, *, boundary_candidate_id: str, snapshot: dict[str, Any]
) -> dict[str, Any]:
    """Build the same task package shape used by the existing external boundary."""
    return {
        "task_type": "domain_boundary_proposal",
        "skill": {
            "formal_skill_id": "domain_boundary_proposal",
            "version": _DOMAIN_BOUNDARY_SKILL_VERSION,
            "source_reference": "cold_start_domain_boundary",
            "content": _proposal_prompt(snapshot),
            "rendered_instructions": _proposal_prompt(snapshot),
            "input_schema": {"type": "object"},
            "output_schema": _DOMAIN_BOUNDARY_OUTPUT_SCHEMA,
            "skill_hash": self._hash({"skill": "domain_boundary_proposal", "version": _DOMAIN_BOUNDARY_SKILL_VERSION}),
            "binding": {
                "name": "domain_boundary_proposal",
                "version": "1.0.0",
                "hash": self._hash({"binding": "domain_boundary_proposal", "version": "1.0.0"}),
            },
        },
        "input": dict(snapshot),
        "constraints": {
            "use_only_supplied_material": True,
            "do_not_search": True,
            "cannot_change_business_state": True,
            "proposal_requires_human_review": True,
        },
        "output_requirements": {
            "submission": "structured_fields",
            "schema": _DOMAIN_BOUNDARY_OUTPUT_SCHEMA,
            "response_format": "structured_fields",
        },
        "business_context": {
            "cold_start_id": str(snapshot["cold_start_id"]),
            "boundary_candidate_id": boundary_candidate_id,
            "data_identity": self.data_identity,
            "origin": "cold_start_domain_boundary",
        },
    }


def _raise_external_intelligence_required(task: dict[str, Any]) -> None:
    # Import lazily because Stage 1B imports the Core that attaches this module.
    from scripts.core.production.stage1b_daily_discovery import ExternalIntelligenceRequired

    raise ExternalIntelligenceRequired(task)


def _record_external_domain_boundary_execution(
    self: Any,
    *,
    boundary_candidate_id: str,
    execution_id: str,
    executor_id: str,
    model_ref: str | None,
    submitted_at: str | None,
    output_payload: dict[str, Any],
) -> str:
    candidate = self.conn.execute(
        "SELECT cold_start_id, source_snapshot_json, status, model_run_json "
        "FROM stage0_cold_start_domain_boundary_candidate "
        "WHERE boundary_candidate_id=? AND data_identity=?",
        (boundary_candidate_id, self.data_identity),
    ).fetchone()
    if candidate is None or str(candidate["status"]) != "preparing":
        raise self.StateTransitionError("domain-boundary external result is stale or mismatched")
    if not str(execution_id or "").strip() or not str(executor_id or "").strip():
        raise self.StateTransitionError("external result requires execution and executor identity")
    if not isinstance(output_payload, dict):
        raise self.StateTransitionError("external result must be structured fields")
    existing_run = json.loads(str(candidate["model_run_json"] or "{}"))
    if isinstance(existing_run, dict) and existing_run:
        raise self.StateTransitionError("this domain-boundary candidate already has an execution result")
    try:
        snapshot = json.loads(str(candidate["source_snapshot_json"] or "{}"))
    except (TypeError, ValueError, json.JSONDecodeError) as exc:
        raise self.StateTransitionError("domain-boundary source snapshot is not valid JSON") from exc
    model_run_id = self._id("domain_boundary_external_execution")
    execution = {
        "model_run_id": model_run_id,
        "status": "succeeded",
        "execution_id": str(execution_id).strip(),
        "executor_id": str(executor_id).strip(),
        "executor_model_ref": str(model_ref or "not_reported").strip() or "not_reported",
        "input_hash": self._hash(snapshot),
        "output_hash": self._hash(output_payload),
        "submitted_at": submitted_at,
        "via_model_gateway": False,
    }
    with self.conn:
        updated = self.conn.execute(
            "UPDATE stage0_cold_start_domain_boundary_candidate SET model_run_json=? "
            "WHERE boundary_candidate_id=? AND data_identity=? AND status='preparing'",
            (self._canonical(execution), boundary_candidate_id, self.data_identity),
        ).rowcount
        if updated != 1:
            raise self.StateTransitionError("domain-boundary external result no longer belongs to a preparing candidate")
        self._audit(
            str(candidate["cold_start_id"]),
            "cold_start_domain_boundary_external_result_recorded",
            {key: value for key, value in execution.items() if key != "output_hash"},
        )
    return model_run_id


def _validate_proposal(
    self: Any, proposal: dict[str, Any], *, snapshot: dict[str, Any], model_used: bool
) -> dict[str, Any]:
    if not isinstance(proposal, dict):
        raise self.StateTransitionError("domain-boundary proposal must be an object")
    valid_refs = {str(item.get("source_id") or "") for item in snapshot.get("valid_observations", [])}
    normalized_sets: list[list[dict[str, Any]]] = []
    seen: set[str] = set()
    for key in ("in_boundary_principles", "out_boundary_principles"):
        raw_items = proposal.get(key)
        if not isinstance(raw_items, list) or not raw_items:
            raise self.StateTransitionError(f"domain-boundary proposal requires {key}")
        normalized: list[dict[str, Any]] = []
        for item in raw_items:
            if not isinstance(item, dict):
                raise self.StateTransitionError("domain-boundary principle must be an object")
            principle = str(item.get("principle") or "").strip()
            rationale = str(item.get("rationale") or "").strip()
            refs = item.get("evidence_refs")
            if len(principle) < 12 or not rationale or not isinstance(refs, list) or not refs:
                raise self.StateTransitionError("domain-boundary principle needs text, rationale and evidence")
            if any(term in principle or term in rationale for term in _BANNED_FORM_TERMS):
                raise self.StateTransitionError("domain-boundary proposal mixed in content type or expression form")
            refs = [str(value).strip() for value in refs if str(value).strip()]
            if not refs or any(value not in valid_refs for value in refs):
                raise self.StateTransitionError("domain-boundary proposal cites a source outside the current run")
            boundary_id = str(item.get("boundary_id") or "").strip() or (
                "boundary_" + self._hash({"polarity": key, "principle": principle})[:20]
            )
            if boundary_id in seen:
                raise self.StateTransitionError("domain-boundary proposal contains duplicate ids")
            seen.add(boundary_id)
            normalized.append({
                "boundary_id": boundary_id,
                "principle": principle,
                "rationale": rationale,
                "evidence_refs": refs,
                "origin": "model_inference" if model_used else "deterministic_isolated_fallback",
                "merged_from": [],
            })
        normalized_sets.append(normalized)
    unknown = proposal.get("unknown_topic_rule")
    if not isinstance(unknown, dict) or not str(unknown.get("rule") or "").strip() or not str(unknown.get("uncertain_action") or "").strip():
        raise self.StateTransitionError("domain-boundary proposal needs an unknown-topic rule")
    return {
        "schema_version": "production_boundary_candidate.v1",
        "model_used": bool(model_used),
        "source_rule": "current_cold_start_successful_breakdown_observations_only",
        "in_boundary_principles": normalized_sets[0],
        "out_boundary_principles": normalized_sets[1],
        "unknown_topic_rule": {
            "rule": str(unknown["rule"]).strip(),
            "uncertain_action": str(unknown["uncertain_action"]).strip(),
            "origin": "model_inference" if model_used else "deterministic_isolated_fallback",
        },
    }


def _accept_external_domain_boundary_result(
    self: Any,
    *,
    boundary_candidate_id: str,
    snapshot: dict[str, Any],
    submission: Mapping[str, Any],
) -> dict[str, Any]:
    if not isinstance(submission, Mapping):
        raise self.StateTransitionError("external intelligent result must be a structured submission")
    output = submission.get("output")
    if not isinstance(output, dict):
        raise self.StateTransitionError("external intelligent result must provide structured output fields")
    execution_id = str(submission.get("execution_id") or "").strip()
    executor_id = str(submission.get("executor_id") or "").strip()
    model_run_id = self.record_cold_start_domain_boundary_external_execution(
        boundary_candidate_id=boundary_candidate_id,
        execution_id=execution_id,
        executor_id=executor_id,
        model_ref=str(submission.get("model_ref") or "").strip() or None,
        submitted_at=str(submission.get("submitted_at") or "").strip() or None,
        output_payload=output,
    )
    try:
        normalized = _validate_proposal(
            self, output, snapshot=snapshot, model_used=True
        )
    except Exception as exc:
        failure = {
            "error_type": type(exc).__name__,
            "reason": str(exc),
            "model_run": {"model_run_id": model_run_id},
            "retry_allowed": True,
            "candidates_created": False,
        }
        with self.conn:
            self.conn.execute(
                "UPDATE stage0_cold_start_domain_boundary_candidate "
                "SET status='failed', failure_json=? "
                "WHERE boundary_candidate_id=? AND data_identity=?",
                (self._canonical(failure), boundary_candidate_id, self.data_identity),
            )
        raise
    with self.conn:
        self.conn.execute(
            "UPDATE stage0_cold_start_domain_boundary_candidate "
            "SET status='awaiting_human_decision', proposal_json=? "
            "WHERE boundary_candidate_id=? AND data_identity=? AND status='preparing'",
            (self._canonical(normalized), boundary_candidate_id, self.data_identity),
        )
        self._audit(
            str(snapshot["cold_start_id"]),
            "cold_start_domain_boundary_candidates_built",
            {
                "boundary_candidate_id": boundary_candidate_id,
                "model_used": True,
                "source_count": len(snapshot["valid_observations"]),
                "model_run_id": model_run_id,
            },
        )
    return get_candidate(self, cold_start_id=str(snapshot["cold_start_id"])) or {}


def submit_cold_start_domain_boundary_external_result(
    self: Any,
    *,
    cold_start_id: str,
    boundary_candidate_id: str,
    execution_id: str,
    executor_id: str,
    model_ref: str | None,
    submitted_at: str | None,
    output: dict[str, Any],
) -> dict[str, Any]:
    candidate = self.conn.execute(
        "SELECT cold_start_id, source_snapshot_json, status "
        "FROM stage0_cold_start_domain_boundary_candidate "
        "WHERE boundary_candidate_id=? AND data_identity=?",
        (boundary_candidate_id, self.data_identity),
    ).fetchone()
    if candidate is None or str(candidate["cold_start_id"]) != str(cold_start_id):
        raise self.StateTransitionError("domain-boundary external result does not belong to the current run")
    snapshot = json.loads(str(candidate["source_snapshot_json"] or "{}"))
    return _accept_external_domain_boundary_result(
        self,
        boundary_candidate_id=boundary_candidate_id,
        snapshot=snapshot,
        submission={
            "execution_id": execution_id,
            "executor_id": executor_id,
            "model_ref": model_ref,
            "submitted_at": submitted_at,
            "output": output,
        },
    )


def _view(self: Any, row: Any) -> dict[str, Any]:
    def read_json(name: str, fallback: Any) -> Any:
        try:
            return json.loads(str(row[name] or ""))
        except (TypeError, ValueError, json.JSONDecodeError):
            return fallback
    return {
        "boundary_candidate_id": str(row["boundary_candidate_id"]),
        "cold_start_id": str(row["cold_start_id"]),
        "domain_label": str(row["domain_label"]),
        "candidate_version": str(row["candidate_version"]),
        "status": str(row["status"]),
        "source_snapshot": read_json("source_snapshot_json", {}),
        "proposal": read_json("proposal_json", {}),
        "failure": read_json("failure_json", {}),
        "review": read_json("review_json", {}),
        "freeze_provenance": read_json("freeze_provenance_json", {}),
        "model_run": read_json("model_run_json", {}),
        "data_identity": str(row["data_identity"]),
        "created_by": str(row["created_by"]),
        "created_at": str(row["created_at"]),
        "reviewed_by": row["reviewed_by"],
        "reviewed_at": row["reviewed_at"],
        "review_reason": row["review_reason"],
    }


def _latest(self: Any, *, cold_start_id: str) -> Any:
    return self.conn.execute(
        "SELECT * FROM stage0_cold_start_domain_boundary_candidate "
        "WHERE cold_start_id=? AND data_identity=? ORDER BY created_at DESC, boundary_candidate_id DESC LIMIT 1",
        (cold_start_id, self.data_identity),
    ).fetchone()


def get_candidate(self: Any, *, cold_start_id: str) -> dict[str, Any] | None:
    row = _latest(self, cold_start_id=cold_start_id)
    return _view(self, row) if row is not None else None


def _normalize_explicit_boundary(self: Any, boundary: Any) -> dict[str, Any]:
    if not isinstance(boundary, Mapping):
        raise self.StateTransitionError(
            "explicit production boundary content must be an object"
        )

    normalized_sets: list[list[dict[str, Any]]] = []
    seen: set[str] = set()
    for key in ("in_boundary_principles", "out_boundary_principles"):
        raw_items = boundary.get(key)
        if not isinstance(raw_items, list) or not raw_items:
            raise self.StateTransitionError(
                f"explicit production boundary requires {key}"
            )
        normalized: list[dict[str, Any]] = []
        for item in raw_items:
            if not isinstance(item, Mapping):
                raise self.StateTransitionError(
                    "explicit production boundary principles must be objects"
                )
            boundary_id = str(item.get("boundary_id") or "").strip()
            principle = str(item.get("principle") or "").strip()
            rationale = str(item.get("rationale") or "").strip()
            if not boundary_id or len(principle) < 12 or not rationale:
                raise self.StateTransitionError(
                    "explicit production boundary principle needs an id, actual content and rationale"
                )
            if boundary_id in seen:
                raise self.StateTransitionError(
                    f"duplicate explicit production boundary {boundary_id!r}"
                )
            seen.add(boundary_id)
            evidence_refs = item.get("evidence_refs", [])
            if not isinstance(evidence_refs, list):
                raise self.StateTransitionError(
                    "explicit production boundary evidence_refs must be an array"
                )
            normalized.append({
                "boundary_id": boundary_id,
                "principle": principle,
                "rationale": rationale,
                "evidence_refs": [
                    str(value).strip()
                    for value in evidence_refs
                    if str(value).strip()
                ],
                "origin": "explicit_human_decision",
                "merged_from": [],
            })
        normalized_sets.append(normalized)

    unknown = boundary.get("unknown_topic_rule")
    if not isinstance(unknown, Mapping):
        raise self.StateTransitionError(
            "explicit production boundary requires an unknown-topic rule"
        )
    rule = str(unknown.get("rule") or "").strip()
    uncertain_action = str(unknown.get("uncertain_action") or "").strip()
    if not rule or not uncertain_action:
        raise self.StateTransitionError(
            "explicit production boundary unknown-topic rule is incomplete"
        )

    return {
        "schema_version": "production_boundary_frozen.v1",
        "source": "explicit_human_decision",
        "in_boundary_principles": normalized_sets[0],
        "out_boundary_principles": normalized_sets[1],
        "unknown_topic_rule": {
            "rule": rule,
            "uncertain_action": uncertain_action,
            "origin": "explicit_human_decision",
        },
    }


def _freeze_explicit_boundary(
    self: Any,
    *,
    cold_start_id: str,
    explicit_boundary: Mapping[str, Any],
    actor: str,
    actor_kind: str,
    reason: str,
    decision_id: str,
) -> dict[str, Any]:
    run = self.conn.execute(
        "SELECT domain_label, status FROM stage0_cold_start "
        "WHERE cold_start_id=? AND data_identity=?",
        (cold_start_id, self.data_identity),
    ).fetchone()
    if run is None:
        raise self.StateTransitionError(
            "explicit production boundary requires the exact current cold-start run"
        )
    if str(run["status"]) not in {"waiting_human", "completed"}:
        raise self.StateTransitionError(
            "explicit production boundary requires a cold-start at the human decision point"
        )
    if actor_kind != "user" or not str(actor or "").strip():
        raise self.StateTransitionError(
            "production-boundary freeze requires an explicit user"
        )
    if not str(reason or "").strip():
        raise self.StateTransitionError(
            "explicit production boundary requires a decision reason"
        )
    if not str(decision_id or "").strip():
        raise self.StateTransitionError(
            "explicit production boundary requires a human decision id"
        )

    final_proposal = _normalize_explicit_boundary(
        self, explicit_boundary
    )
    domain_label = str(run["domain_label"])
    existing_registry = get_production_boundary_registry(domain_label)
    if str(existing_registry.get("status") or "").casefold() == "frozen":
        raise self.StateTransitionError(
            f"domain {domain_label} already has a frozen production boundary"
        )

    now = self._now()
    provenance = {
        "source": "explicit_human_decision",
        "cold_start_id": cold_start_id,
        "human_decision_id": str(decision_id),
        "frozen_at": now,
    }
    domain_pack = self.get_domain_pack(domain_label)
    pack_path = Path(str(domain_pack["config_path"])).resolve()
    original_pack = pack_path.read_text(encoding="utf-8")
    try:
        registry = freeze_production_boundary_registry(
            domain_label,
            in_boundary_principles=final_proposal["in_boundary_principles"],
            out_boundary_principles=final_proposal["out_boundary_principles"],
            unknown_topic_rule=final_proposal["unknown_topic_rule"],
            provenance=provenance,
        )
        with self.conn:
            self._audit(None, "cold_start_domain_boundary_frozen", {
                "cold_start_id": cold_start_id,
                "domain_label": domain_label,
                "source": "explicit_human_decision",
                "boundary_version": registry["version"],
                "human_decision_id": str(decision_id),
            })
    except Exception:
        try:
            pack_path.write_text(original_pack, encoding="utf-8")
        except Exception:
            pass
        raise

    result = {
        "cold_start_id": cold_start_id,
        "domain_label": domain_label,
        "status": "frozen",
        "source": "explicit_human_decision",
        "proposal": final_proposal,
        "review": {
            "source": "explicit_human_decision",
            "reason": str(reason),
        },
        "freeze_provenance": provenance,
        "model_run": {},
        "data_identity": self.data_identity,
    }
    result["cold_start_completion"] = self.try_complete_cold_start(
        cold_start_id=cold_start_id,
        trigger="production_boundary_freeze",
        actor=actor,
    )
    return result


def build_candidates(
    self: Any, *, cold_start_id: str, actor: str | None = None,
    proposal_generator: Callable[[dict[str, Any]], dict[str, Any]] | None = None,
    external_executor: Callable[[dict[str, Any]], Mapping[str, Any]] | None = None,
) -> dict[str, Any]:
    if not str(cold_start_id or "").strip():
        raise self.StateTransitionError("domain-boundary candidates require a current run")
    existing = _latest(self, cold_start_id=cold_start_id)
    if existing is not None and str(existing["status"]) in {"awaiting_human_decision", "frozen"}:
        return _view(self, existing)
    if existing is not None and str(existing["status"]) == "preparing":
        snapshot = json.loads(str(existing["source_snapshot_json"] or "{}"))
        task = _domain_boundary_external_task(
            self,
            boundary_candidate_id=str(existing["boundary_candidate_id"]),
            snapshot=snapshot,
        )
        if external_executor is None:
            _raise_external_intelligence_required(task)
        return _accept_external_domain_boundary_result(
            self,
            boundary_candidate_id=str(existing["boundary_candidate_id"]),
            snapshot=snapshot,
            submission=external_executor(task),
        )
    snapshot = _observation_snapshot(self, cold_start_id=cold_start_id)
    latest = self.conn.execute(
        "SELECT candidate_version FROM stage0_cold_start_domain_boundary_candidate "
        "WHERE cold_start_id=? AND data_identity=? ORDER BY created_at DESC LIMIT 1",
        (cold_start_id, self.data_identity),
    ).fetchone()
    version = str(int(latest["candidate_version"]) + 1) if latest and str(latest["candidate_version"]).isdigit() else "1"
    candidate_id, now = self._id("domain_boundary_candidate"), self._now()
    with self.conn:
        self.conn.execute(
            "INSERT INTO stage0_cold_start_domain_boundary_candidate "
            "(boundary_candidate_id, cold_start_id, domain_label, candidate_version, status, source_snapshot_json, "
            "proposal_json, failure_json, review_json, freeze_provenance_json, model_run_json, data_identity, "
            "created_by, created_at, reviewed_by, reviewed_at, review_reason) "
            "VALUES (?, ?, ?, ?, 'preparing', ?, '{}', '{}', '{}', '{}', '{}', ?, ?, ?, NULL, NULL, NULL)",
            (candidate_id, cold_start_id, snapshot["domain_label"], version, self._canonical(snapshot), self.data_identity, "", now),
        )
    from scripts.core.production.stage1b_daily_discovery import ExternalIntelligenceRequired
    try:
        if not snapshot["valid_observations"]:
            raise self.StateTransitionError("current cold-start run has no usable successful breakdown evidence")
        if proposal_generator is not None:
            proposal, model_used = proposal_generator(snapshot), True
        elif self.data_identity == "production":
            task = _domain_boundary_external_task(
                self, boundary_candidate_id=candidate_id, snapshot=snapshot
            )
            if external_executor is None:
                _raise_external_intelligence_required(task)
            return _accept_external_domain_boundary_result(
                self,
                boundary_candidate_id=candidate_id,
                snapshot=snapshot,
                submission=external_executor(task),
            )
        else:
            proposal, model_used = _default_isolated_proposal(snapshot), False
        normalized = _validate_proposal(self, proposal, snapshot=snapshot, model_used=model_used)
    except ExternalIntelligenceRequired:
        raise
    except Exception as exc:
        row = self.conn.execute(
            "SELECT model_run_json FROM stage0_cold_start_domain_boundary_candidate WHERE boundary_candidate_id=? AND data_identity=?",
            (candidate_id, self.data_identity),
        ).fetchone()
        try:
            model_run = json.loads(str(row["model_run_json"] or "{}")) if row else {}
        except (TypeError, ValueError, json.JSONDecodeError):
            model_run = {}
        failure = {"error_type": type(exc).__name__, "reason": str(exc), "model_run": model_run, "retry_allowed": True, "candidates_created": False}
        with self.conn:
            self.conn.execute(
                "UPDATE stage0_cold_start_domain_boundary_candidate SET status='failed', failure_json=? WHERE boundary_candidate_id=? AND data_identity=?",
                (self._canonical(failure), candidate_id, self.data_identity),
            )
            self._audit(None, "cold_start_domain_boundary_candidate_failed", {"cold_start_id": cold_start_id, "boundary_candidate_id": candidate_id, "reason": str(exc)})
        return get_candidate(self, cold_start_id=cold_start_id) or {}
    with self.conn:
        self.conn.execute(
            "UPDATE stage0_cold_start_domain_boundary_candidate SET status='awaiting_human_decision', proposal_json=? WHERE boundary_candidate_id=? AND data_identity=?",
            (self._canonical(normalized), candidate_id, self.data_identity),
        )
        self._audit(None, "cold_start_domain_boundary_candidates_built", {"cold_start_id": cold_start_id, "boundary_candidate_id": candidate_id, "candidate_version": version, "model_used": model_used, "source_count": len(snapshot["valid_observations"])})
    return get_candidate(self, cold_start_id=cold_start_id) or {}


def review_boundary(
    self: Any, *, cold_start_id: str, decisions: tuple[dict[str, Any], ...], actor: str,
    actor_kind: str, reason: str, decision_id: str = "", unknown_topic_rule: dict[str, Any] | None = None,
    explicit_boundary: Mapping[str, Any] | None = None,
) -> dict[str, Any]:
    current = get_candidate(self, cold_start_id=cold_start_id)
    if current is None:
        if explicit_boundary is None:
            raise self.StateTransitionError(
                "no boundary candidate exists; explicit production boundary content is required"
            )
        return _freeze_explicit_boundary(
            self,
            cold_start_id=cold_start_id,
            explicit_boundary=explicit_boundary,
            actor=actor,
            actor_kind=actor_kind,
            reason=reason,
            decision_id=decision_id,
        )
    if explicit_boundary is not None:
        raise self.StateTransitionError(
            "a boundary candidate already exists; review the existing candidate"
        )
    if current["status"] == "frozen":
        result = dict(current)
        result["cold_start_completion"] = self.try_complete_cold_start(
            cold_start_id=cold_start_id,
            trigger="production_boundary_freeze",
            actor=actor,
        )
        return result
    if current["status"] != "awaiting_human_decision":
        raise self.StateTransitionError("domain-boundary candidates are not awaiting human decision")
    if actor_kind != "user":
        raise self.StateTransitionError("production-boundary freeze requires an explicit user")
    proposal = current["proposal"]
    original: dict[str, dict[str, Any]] = {}
    polarity: dict[str, str] = {}
    for key, value in (("in", proposal.get("in_boundary_principles", [])), ("out", proposal.get("out_boundary_principles", []))):
        for item in value:
            original[str(item["boundary_id"])] = dict(item)
            polarity[str(item["boundary_id"])] = key
    submitted: dict[str, dict[str, Any]] = {}
    additions: list[dict[str, Any]] = []
    for item in decisions:
        if not isinstance(item, dict):
            raise self.StateTransitionError("every boundary decision must be an object")
        boundary_id, decision = str(item.get("boundary_id") or "").strip(), str(item.get("decision") or "").strip()
        if not boundary_id or decision not in {"accepted", "rejected", "merged"}:
            raise self.StateTransitionError("boundary decisions must accept, reject or merge a principle")
        if boundary_id in original:
            if boundary_id in submitted:
                raise self.StateTransitionError("a boundary principle was reviewed twice")
            submitted[boundary_id] = dict(item)
        else:
            if decision != "accepted" or str(item.get("origin") or "").strip() not in {"user_confirmation", "user_added"}:
                raise self.StateTransitionError("new boundary principles may only be added by the user")
            additions.append(dict(item))
    if set(submitted) != set(original):
        raise self.StateTransitionError("the whole candidate boundary set must be reviewed in one submission")
    final: dict[str, dict[str, Any]] = {}
    final_polarity: dict[str, str] = {}
    for boundary_id, original_item in original.items():
        item = submitted[boundary_id]
        if item["decision"] == "rejected":
            continue
        if item["decision"] == "merged":
            target = str(item.get("merge_into") or "").strip()
            if target not in original or target == boundary_id:
                raise self.StateTransitionError("merged boundary must name another candidate")
            continue
        edited = dict(original_item)
        edited["principle"] = str(item.get("edited_principle") or edited["principle"]).strip()
        edited["rationale"] = str(item.get("edited_rationale") or edited["rationale"]).strip()
        final[boundary_id], final_polarity[boundary_id] = edited, str(item.get("polarity") or polarity[boundary_id])
    for item in additions:
        boundary_id = str(item["boundary_id"])
        principle = str(item.get("principle") or item.get("edited_principle") or "").strip()
        rationale = str(item.get("rationale") or item.get("edited_rationale") or "").strip()
        direction = str(item.get("polarity") or "").strip()
        if boundary_id in final or len(principle) < 12 or not rationale or direction not in {"in", "out"}:
            raise self.StateTransitionError("user-added boundary needs a unique principle, rationale and polarity")
        final[boundary_id] = {"boundary_id": boundary_id, "principle": principle, "rationale": rationale, "evidence_refs": [], "origin": "user_confirmation", "merged_from": []}
        final_polarity[boundary_id] = direction
    for item in decisions:
        if str(item.get("decision") or "") == "merged":
            target = str(item.get("merge_into") or "").strip()
            if target in final:
                final[target].setdefault("merged_from", []).append(str(item.get("boundary_id") or ""))
    in_final = [dict(item) for key, item in final.items() if final_polarity[key] == "in"]
    out_final = [dict(item) for key, item in final.items() if final_polarity[key] == "out"]
    if not in_final or not out_final:
        raise self.StateTransitionError("final production boundary needs both in and out principles")
    unknown = dict(proposal.get("unknown_topic_rule") or {})
    if unknown_topic_rule is not None:
        if not isinstance(unknown_topic_rule, dict) or not str(unknown_topic_rule.get("rule") or "").strip():
            raise self.StateTransitionError("edited unknown-topic rule is incomplete")
        unknown.update({"rule": str(unknown_topic_rule["rule"]).strip(), "uncertain_action": str(unknown_topic_rule.get("uncertain_action") or "待用户确认").strip(), "origin": "user_confirmation"})
    now = self._now()
    provenance = {"cold_start_id": cold_start_id, "boundary_candidate_id": current["boundary_candidate_id"], "candidate_version": current["candidate_version"], "human_decision_id": str(decision_id or ""), "frozen_at": now}
    final_proposal = {"schema_version": "production_boundary_frozen.v1", "in_boundary_principles": in_final, "out_boundary_principles": out_final, "unknown_topic_rule": unknown}
    domain_pack = self.get_domain_pack(current["domain_label"])
    pack_path = Path(str(domain_pack["config_path"])).resolve()
    original_pack = pack_path.read_text(encoding="utf-8")
    try:
        registry = freeze_production_boundary_registry(current["domain_label"], in_boundary_principles=in_final, out_boundary_principles=out_final, unknown_topic_rule=unknown, provenance=provenance)
        with self.conn:
            self.conn.execute(
                "UPDATE stage0_cold_start_domain_boundary_candidate SET status='frozen', proposal_json=?, review_json=?, freeze_provenance_json=?, reviewed_by=?, reviewed_at=?, review_reason=? WHERE boundary_candidate_id=? AND data_identity=? AND status='awaiting_human_decision'",
                (self._canonical(final_proposal), self._canonical({"decisions": [dict(item) for item in decisions], "reason": reason}), self._canonical(provenance), None, now, reason, current["boundary_candidate_id"], self.data_identity),
            )
            self._audit(None, "cold_start_domain_boundary_frozen", {"cold_start_id": cold_start_id, "boundary_candidate_id": current["boundary_candidate_id"], "candidate_version": current["candidate_version"], "boundary_version": registry["version"], "human_decision_id": str(decision_id or "")})
    except Exception:
        try:
            pack_path.write_text(original_pack, encoding="utf-8")
        except Exception:
            pass
        raise
    result = get_candidate(self, cold_start_id=cold_start_id) or {}
    result["cold_start_completion"] = self.try_complete_cold_start(
        cold_start_id=cold_start_id,
        trigger="production_boundary_freeze",
        actor="system",
    )
    return result


def boundary_is_frozen(self: Any, *, cold_start_id: str) -> bool:
    current = get_candidate(self, cold_start_id=cold_start_id)
    if current is not None:
        if current["status"] != "frozen":
            return False
        provenance = current.get("freeze_provenance") or {}
        if str(provenance.get("cold_start_id") or "") != cold_start_id:
            return False
        try:
            registry = require_frozen_production_boundary(current["domain_label"])
        except ValueError:
            return False
        registry_provenance = registry.get("provenance") or {}
        return (
            str(registry_provenance.get("cold_start_id") or "") == cold_start_id
            and str(registry_provenance.get("boundary_candidate_id") or "")
            == str(current["boundary_candidate_id"])
        )

    run = self.conn.execute(
        "SELECT domain_label FROM stage0_cold_start "
        "WHERE cold_start_id=? AND data_identity=?",
        (cold_start_id, self.data_identity),
    ).fetchone()
    if run is None:
        return False
    try:
        registry = require_frozen_production_boundary(str(run["domain_label"]))
    except ValueError:
        return False
    provenance = registry.get("provenance") or {}
    return (
        str(provenance.get("source") or "") == "explicit_human_decision"
        and str(provenance.get("cold_start_id") or "") == cold_start_id
    )


def production_boundary_for_qualification(self: Any, *, domain_label: str, require_frozen: bool = True) -> dict[str, Any]:
    return require_frozen_production_boundary(domain_label) if require_frozen else get_production_boundary_registry(domain_label)


def evaluate_current_boundary(self: Any, *, domain_label: str, outcome: str, matched_principle_ids: list[str] | tuple[str, ...] = (), violated_principle_ids: list[str] | tuple[str, ...] = (), basis: str) -> dict[str, Any]:
    return evaluate_production_boundary(domain_label, outcome=outcome, matched_principle_ids=matched_principle_ids, violated_principle_ids=violated_principle_ids, basis=basis)


def attach_core_methods(core_cls: type[Any]) -> None:
    # Reuse the existing Core primitives; this module adds lifecycle methods,
    # not a second persistence or error model.
    from scripts.core.production.stage0_content_core import (
        StateTransitionError,
        _canonical,
        _hash,
        _id,
        _now,
    )
    from scripts.core.business_data.domain_labels import get_domain_pack

    core_cls.StateTransitionError = StateTransitionError
    core_cls._canonical = staticmethod(_canonical)
    core_cls._hash = staticmethod(_hash)
    core_cls._id = staticmethod(_id)
    core_cls._now = staticmethod(_now)
    core_cls.get_domain_pack = staticmethod(get_domain_pack)

    core_cls.get_cold_start_domain_boundary_observations = _observation_snapshot
    core_cls.get_cold_start_domain_boundary_candidate = get_candidate
    core_cls.build_cold_start_domain_boundary_candidates = build_candidates
    core_cls.record_cold_start_domain_boundary_external_execution = _record_external_domain_boundary_execution
    core_cls.submit_cold_start_domain_boundary_external_result = submit_cold_start_domain_boundary_external_result
    core_cls.review_cold_start_domain_boundary = review_boundary
    core_cls.cold_start_domain_boundary_is_frozen = boundary_is_frozen
    core_cls.get_production_boundary_for_qualification = production_boundary_for_qualification
    core_cls.evaluate_current_production_boundary = evaluate_current_boundary

