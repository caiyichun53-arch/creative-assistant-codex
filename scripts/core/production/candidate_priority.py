"""Candidate priority: Agent judges the supplied pool; Core owns writes and ordering."""
from __future__ import annotations

import hashlib
import json
from datetime import datetime, timezone

from scripts.core.model_gateway.formal_skill_adapter import (
    FormalSkillContract, prepare_external_skill_task, validate_external_skill_output,
)
from scripts.core.business_data.domain_boundaries import topic_domain_rules


def canonical(value):
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def packet(core, *, run_id, domain_label):
    run = core._discovery_run(run_id)
    context = core._discovery_context(run_id)
    if core.conn.execute("SELECT 1 FROM stage1b_run_domain_scope WHERE run_id=? AND domain_label=? AND data_identity=?", (run_id, domain_label, core.data_identity)).fetchone() is None:
        raise ValueError("candidate priority domain is outside this discovery run")
    rows = core.conn.execute(
        "SELECT c.* FROM stage1b_candidate_version c "
        "WHERE c.data_identity=? AND c.domain_label=? AND c.status='awaiting_user_decision' "
        "AND (SELECT p.pool_status FROM stage1b_candidate_pool_state p "
        "WHERE p.candidate_version_id=c.candidate_version_id AND p.data_identity=c.data_identity "
        "ORDER BY p.effective_at DESC,p.pool_state_id DESC LIMIT 1)='current' "
        "AND NOT EXISTS (SELECT 1 FROM stage1b_candidate_decision d WHERE d.candidate_version_id=c.candidate_version_id) "
        "ORDER BY c.created_at,c.candidate_version_id", (core.data_identity, domain_label),
    ).fetchall()
    candidates = []
    for row in rows:
        sources = core.conn.execute(
            "SELECT s.source_version_id,s.source_type,s.source_time,s.expires_at,s.payload_json "
            "FROM stage1b_source_version s WHERE s.data_identity=? AND "
            "(s.source_version_id=? OR s.source_version_id IN "
            "(SELECT source_version_id FROM stage1b_candidate_support WHERE candidate_version_id=? AND data_identity=?)) "
            "ORDER BY s.source_version_id",
            (core.data_identity,row["source_version_id"],row["candidate_version_id"],core.data_identity),
        ).fetchall()
        materials = []
        for source in sources:
            recorded = core.conn.execute(
                "SELECT a.payload_json FROM stage0_audit_event a "
                "JOIN stage1b_model_run m ON m.model_run_id=json_extract(a.payload_json,'$.model_run_id') "
                "WHERE a.action='stage1b_external_intelligence_result_recorded' "
                "AND m.source_version_id=? AND m.data_identity=? AND m.validation_status!='failed' "
                "ORDER BY m.created_at DESC, m.model_run_id DESC LIMIT 1",
                (source["source_version_id"], core.data_identity),
            ).fetchone()
            materials.append({**{k:source[k] for k in source.keys() if k != "payload_json"},
                              "payload":json.loads(source["payload_json"]),
                              "topic_formation":json.loads(recorded["payload_json"]).get("output") if recorded else None})
        candidates.append({"candidate_version_id": row["candidate_version_id"],
                           "candidate": json.loads(row["payload_json"]),
                           "created_at": row["created_at"],
                           "materials": materials})
    return {"run_id":run_id, "domain_label":domain_label, "discovery_date":run["discovery_date"],
            "candidates":candidates, "domain_rules":topic_domain_rules(domain_label)}


def prepare(core, *, run_id, domain_label):
    supplied = packet(core, run_id=run_id, domain_label=domain_label)
    supplied["input_fingerprint"] = hashlib.sha256(canonical(supplied).encode()).hexdigest()
    task, _ = prepare_external_skill_task(
        FormalSkillContract.from_runtime_skill("candidate_priority"), supplied,
        constraints={"model_selection":"current_agent", "external_search":False,
                     "formal_business_write":"Core_only", "human_selection_required":True},
    )
    task["task_type"] = "candidate_priority"
    task["task_identity"] = {"run_id":run_id,"domain_label":domain_label}
    return task


def submit(core, *, run_id, domain_label, output, execution_id, executor_id, model_ref=None):
    core._discovery_run(run_id)
    request = {"run_id":run_id,"domain_label":domain_label,"output":output,
               "execution_id":execution_id,"executor_id":executor_id,"model_ref":model_ref}
    if core._discovery_run(run_id)["status"] != "processing":
        raise ValueError("candidate priority can only be submitted to a processing discovery run")
    if not execution_id.strip() or not executor_id.strip():
        raise ValueError("external execution provenance is required")
    task = prepare(core,run_id=run_id,domain_label=domain_label)
    supplied = task["input"]
    value = validate_external_skill_output(FormalSkillContract.from_runtime_skill("candidate_priority"), supplied, output)
    if value["input_fingerprint"] != supplied["input_fingerprint"]:
        raise ValueError("candidate pool or supporting material changed; score the current input")
    replay = core._replay("candidate_priority_submit", execution_id, request)
    if replay:
        return replay
    candidates = {c["candidate_version_id"]:c for c in supplied["candidates"]}
    seen = set()
    for assessment in value["assessments"]:
        cid = assessment["candidate_version_id"]
        if cid not in candidates or cid in seen:
            raise ValueError("candidate assessments must match the supplied pool exactly")
        seen.add(cid)
        if type(assessment["score"]) is not int or not 0 <= assessment["score"] <= 100:
            raise ValueError("candidate priority must be an integer from 0 to 100")
        if not assessment["reason"].strip() or not assessment["limitations"].strip():
            raise ValueError("candidate priority needs a concrete reason and limitations")
        refs = {m["source_version_id"] for m in candidates[cid]["materials"]}
        if not assessment["evidence_refs"] or not set(assessment["evidence_refs"]) <= refs:
            raise ValueError("assessment evidence must refer to this candidate's supplied materials")
    if seen != set(candidates):
        raise ValueError("every candidate must receive an assessment; Top 10 is only a display view")
    now = datetime.now(timezone.utc).isoformat()
    with core.conn:
        core.conn.execute("DELETE FROM stage1b_candidate_priority WHERE run_id=? AND domain_label=? AND data_identity=?",
                          (run_id,domain_label,core.data_identity))
        for assessment in value["assessments"]:
            cid = assessment["candidate_version_id"]
            core.conn.execute(
                "INSERT INTO stage1b_candidate_priority VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                (run_id,domain_label,cid,supplied["input_fingerprint"],canonical(candidates[cid]),
                 assessment["score"],assessment["reason"],assessment["limitations"],canonical(assessment["evidence_refs"]),
                 executor_id,model_ref,execution_id,core.data_identity,now),
            )
        core._audit(run_id,"candidate_priority_scored",{"domain_label":domain_label,"input_fingerprint":supplied["input_fingerprint"],
                                                     "execution_id":execution_id,"executor_id":executor_id,"model_ref":model_ref,"count":len(seen)})
        result = {"run_id":run_id,"domain_label":domain_label,"assessed_count":len(seen),"input_fingerprint":supplied["input_fingerprint"]}
        core._receipt("candidate_priority_submit", execution_id, request, result)
    return result


def ranked(core, *, run_id, domain_label):
    task = prepare(core,run_id=run_id,domain_label=domain_label)
    candidates = task["input"]["candidates"]
    rows = core.conn.execute("SELECT * FROM stage1b_candidate_priority WHERE run_id=? AND domain_label=? AND data_identity=? ORDER BY total_score DESC,candidate_version_id",
                             (run_id,domain_label,core.data_identity)).fetchall()
    if {r["candidate_version_id"] for r in rows} != {c["candidate_version_id"] for c in candidates} or any(
        r["input_fingerprint"] != task["input"]["input_fingerprint"] for r in rows
    ):
        return None, task
    created = {c["candidate_version_id"]:c["created_at"] for c in candidates}
    return sorted(rows,key=lambda r:(-r["total_score"],created[r["candidate_version_id"]],r["candidate_version_id"])), task


def assessment_is_current(core, assessment):
    if assessment is None:
        return False
    candidate = core._discovery_candidate(assessment["candidate_version_id"])
    frozen = json.loads(assessment["candidate_input_json"])
    refs = {r[0] for r in core.conn.execute(
        "SELECT source_version_id FROM stage1b_candidate_support WHERE candidate_version_id=? AND data_identity=?",
        (candidate["candidate_version_id"], core.data_identity))}
    refs.add(candidate["source_version_id"])
    return (json.loads(candidate["payload_json"]) == frozen["candidate"]
            and refs == {m["source_version_id"] for m in frozen["materials"]})


def report(core, *, run_id, domain_label):
    core._discovery_run(run_id)
    rows = core.conn.execute(
        "SELECT * FROM stage1b_candidate_priority WHERE run_id=? AND domain_label=? AND data_identity=?",
        (run_id, domain_label, core.data_identity)).fetchall()
    rows = sorted(rows,key=lambda r:(-r["total_score"],json.loads(r["candidate_input_json"])["created_at"],r["candidate_version_id"]))
    return {"run_id":run_id,"domain_label":domain_label,"score_meaning":"Agent 制作优先级判断，不是热度或播放量预测",
            "status":core._discovery_run(run_id)["status"],
            "candidates":[{"candidate_version_id":r["candidate_version_id"],
                           "candidate":json.loads(r["candidate_input_json"])["candidate"],
                           "score":r["total_score"],"reason":r["reason"],"limitations":r["limitations"],
                           "evidence_refs":json.loads(r["evidence_refs_json"]),"assessed_by":r["assessed_by"],
                           "model_ref":r["model_ref"],"current_materials":assessment_is_current(core,r)} for r in rows]}


def install_schema(core):
    core.conn.executescript("""
        BEGIN;
            CREATE TABLE IF NOT EXISTS stage1b_candidate_priority (
                run_id TEXT NOT NULL REFERENCES stage1b_discovery_run(run_id),
                domain_label TEXT NOT NULL,
                candidate_version_id TEXT NOT NULL REFERENCES stage1b_candidate_version(candidate_version_id),
                input_fingerprint TEXT NOT NULL,
                candidate_input_json TEXT NOT NULL,
                total_score INTEGER NOT NULL CHECK(total_score BETWEEN 0 AND 100),
                reason TEXT NOT NULL,
                limitations TEXT NOT NULL,
                evidence_refs_json TEXT NOT NULL,
                assessed_by TEXT NOT NULL,
                model_ref TEXT,
                execution_id TEXT NOT NULL,
                data_identity TEXT NOT NULL,
                assessed_at TEXT NOT NULL,
                PRIMARY KEY(run_id,candidate_version_id)
            );
            DROP TABLE IF EXISTS stage1b_candidate_assessment_revision;
            DROP TABLE IF EXISTS stage1b_candidate_assessment;
        COMMIT;
    """)
