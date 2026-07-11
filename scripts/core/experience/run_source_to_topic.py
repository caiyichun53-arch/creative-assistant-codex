"""Core-side wiring between hit_deep_analysis (sample_deep_analyze's real
output, see run_sample_deep_analyze.py) and the runtime_skills/source_to_topic
atomic Skill (SOURCE_TO_TOPIC_BUSINESS_CONTRACT.yaml).

2026-07-09: this is the second of three planned bindings for the "选题 ->
大纲 -> 成稿" chain (see ROADMAP.md) -- source_to_topic -> content_plan ->
script_generate. Follows the exact division-of-responsibility pattern
established by run_sample_deep_analyze.py (that file's module docstring
explains the pattern in full; not repeated here).

What counts as "source evidence" here: source_to_topic's own business
contract (non_responsibilities: deep_sample_analysis, tactic_extract) makes
clear the Skill itself must not know or care where its evidence came from --
that decision belongs entirely to this binding layer. As of 2026-07-13(D1),
three of the four 选题 methodology evidence sources are real: 对标爆款
(hit_deep_analysis), 评论区 (hit_comments), 当下热点 (hotspot_events, A6's
manual registration, auto-included -- see _hotspot_evidence_items()).
研究缺口(the fourth) stays unwired -- the concept itself does not have a
clear, authoritative current-design definition anywhere in this repo as of
this writing (checked the interactive-session-tooling "选题" skill writeup,
which explicitly disclaims itself as a stale reference, and the other coding-
tool's agent definitions, which have no topic-selection definition at all);
guessing at it would violate this project's "no invented business logic"
discipline.

2026-07-13(D2/D3, BR-TOPIC-001): the "筛选去重+冷却+领域约束" this rule
requires (previously `pending_implementation`) is now real, deterministic
code, not a scoring/ranking mechanism:
  - 领域约束: _apply_domain_constraint() -- the only check is whether the
    account's domain_label fell outside ALLOWED_DOMAIN_LABELS; no semantic
    "does this content really fit the domain" judgement (that would need an
    LLM score, which BR-TOPIC-001 forbids).
  - 去重/冷却: check_topic_candidate_duplicate() -- the first real binding of
    content_relation_judge (its contract existed with zero real callers
    before this). Compares a newly generated candidate against the same
    account's recent candidates; a same_item/equivalent/contains/
    contained_by result forces human review rather than auto-rejecting
    (BR-TOPIC-004).

Explicitly NOT built here (acknowledged gap, matching the source_to_topic
contract's own non_responsibilities, not an oversight):
  - relation_summary (the INPUT field fed to source_to_topic itself,
    describing "has a relation check already been done for this candidate
    before generation") is still the plain, honest NO_RELATION_JUDGEMENT_YET
    placeholder -- this is still accurate, not stale: check_topic_candidate_
    duplicate() runs AFTER a candidate is generated and persisted, so by
    construction no relation result exists yet at generation time to report
    back into this input field.

Usage:
    python -m scripts.core.experience.run_source_to_topic --limit 1
"""

from __future__ import annotations

import argparse
import json
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any
import sqlite3

ROOT = Path(__file__).resolve().parents[3]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from scripts.core.business_data.domain_labels import ALLOWED_DOMAIN_LABELS  # noqa: E402
from scripts.core.business_data.register_competitor_accounts import DEFAULT_DB, install_schema  # noqa: E402
from scripts.core.business_data.run_domain_search import install_schema as install_domain_search_schema  # noqa: E402
from scripts.core.execution_contract import require_catalog_citations  # noqa: E402
from scripts.core.experience.run_sample_deep_analyze import _load_env_value, _safe_db_path  # noqa: E402
from scripts.core.model_gateway.formal_skill_adapter import (  # noqa: E402
    FormalBusinessSkillHarness,
    ModelRoute,
    make_content_relation_judge_harness,
    make_source_to_topic_harness,
)
from scripts.core.model_gateway.hermes_model_provider import HermesModelProviderAdapter, HermesModelProviderConfig  # noqa: E402
from scripts.core.persistence.goal01_store import content_hash  # noqa: E402
# 字符上限(SOURCE_CONTENT_MAX_CHARS/EVIDENCE_ITEM_MAX_CHARS/
# RELATION_SUMMARY_MAX_CHARS)2026-07-13 用户明确拍板彻底取消,真实内容一律
# 原样完整发给模型。
NO_RELATION_JUDGEMENT_YET = (
    "尚未做过来源关联判断(content_relation_judge 这个 Skill 还没有接上真实数据)——"
    "本次候选选题只依据下面列出的证据本身生成,不代表已经和现有内容/选题库比对过是否重复或冲突。"
)
# 2026-07-10: comments are the second of four evidence sources the project's
# own 选题 methodology (the topic-selection session skill's writeup, see
# ROADMAP.md for the exact reference) calls for -- "评论区（最值钱）：来源爆款
# 评论里观众反复追问/争论/喊你该讲讲X的 → 直接立题" -- real hit_comments data
# already existed (collected during reverse-prep) but was never used here
# before. \x1e (record separator) cannot appear in real crawled text, unlike
# a delimiter like '||' a comment could plausibly contain.
TOP_COMMENTS_DELIMITER = "\x1e"
# 2026-07-11: the UNSOURCED "3" per-hit comment cap that used to live here is
# gone -- explicit user decision. Re-imposing a second, arbitrary, smaller
# cap on top of an already-real limit made no sense: reverse-prep's own
# comment collection is already bounded by BR-COLLECT-005/006's real
# top_comments=60 (see run_reverse_prep.py's install-time assertion), so
# every real hit already has at most 60 comments to draw from -- no second
# cutoff invented here. PATTERN_EVIDENCE_ITEM_COUNT (3: 选题/开头/结构 手法)
# + 60 (the real upstream ceiling) = SOURCE_EVIDENCE_ITEMS_MAX below, a
# derived number, not a guess -- see that constant and the schema files it
# mirrors for the full accounting.
PATTERN_EVIDENCE_ITEM_COUNT = 3
REVERSE_PREP_MAX_COMMENTS_PER_HIT = 60  # BR-COLLECT-005/006, see run_reverse_prep.py
SOURCE_EVIDENCE_ITEMS_MAX = PATTERN_EVIDENCE_ITEM_COUNT + REVERSE_PREP_MAX_COMMENTS_PER_HIT

# 2026-07-13(D1/D2/D3, BR-TOPIC-001/BR-TOPIC-006):这三个数字都是这次会话给的
# 合理默认,不是文档依据——HOTSPOT_EVIDENCE_WINDOW_DAYS 特意跟 D3 的去重冷却窗口
# 用同一个 30,保持口径一致,不是另起一个不相关的数字;DEDUP_CANDIDATE_WINDOW_DAYS/
# DEDUP_CANDIDATE_COMPARE_MAX 参照这次会话里其它地方反复出现的 30天/20条上限
# (A5的30天账号复查窗口、A3的search_read_max/tactic_extract的dna_note_refs_max)。
HOTSPOT_EVIDENCE_WINDOW_DAYS = 30
DEDUP_CANDIDATE_WINDOW_DAYS = 30
DEDUP_CANDIDATE_COMPARE_MAX = 20
# BR-TOPIC-001 的"重复"定义:9种 content_relation_judge 关系类型里,只有这四种
# 算实质重复(命中任一种就要求人工复核,不自动拒绝)。complementary/contradicts/
# related_distinct/no_relation/insufficient_evidence 都不算——尤其 contradicts,
# 两条内容相互矛盾不是重复,可能恰恰是两个值得单独报的角度。
DUPLICATE_RELATION_TYPES = frozenset({"same_item", "equivalent", "contains", "contained_by"})


def validate_source_to_topic_execution_contract() -> dict[str, Any]:
    return require_catalog_citations(["BR-DNA-001", "BR-TOPIC-001", "BR-TOPIC-006"])


def select_analyses_pending_topic(conn: sqlite3.Connection, *, limit: int) -> list[sqlite3.Row]:
    """Real hit_deep_analysis rows (sample_deep_analyze's completed output)
    that have never been through source_to_topic (no topic_candidates row
    yet). Oldest-created first, same ordering convention as
    select_hits_pending_analysis().

    2026-07-10: also pulls this hit's real comments (by the crawler's own
    hotness ordering, hit_comments.sample_rank -- lower is hotter), joined as
    one delimited column rather than a second query, so
    assemble_source_to_topic_input() can stay a pure function that only reads
    the row it's given. TOP_COMMENTS_DELIMITER is a control character that
    cannot appear in real comment text, not '||' or similar (which a comment
    could plausibly, if rarely, contain). 2026-07-11: no LIMIT here anymore
    (explicit user decision) -- reverse-prep already bounds real comment
    count per hit to BR-COLLECT-005/006's real top_comments=60, so a second,
    smaller, arbitrary cutoff here just duplicated an already-real limit for
    no reason."""
    return conn.execute(
        """
        SELECT hit_deep_analysis.*, hits.title AS hit_title, account.domain_label AS account_domain_label,
               account.account_name AS account_name,
               (SELECT GROUP_CONCAT(text, ?) FROM (
                    SELECT text FROM hit_comments
                     WHERE hit_comments.hit_id = hits.hit_id
                     ORDER BY sample_rank
                )) AS top_comments_text
          FROM hit_deep_analysis
          JOIN hits ON hits.hit_id = hit_deep_analysis.hit_id
          JOIN competitor_accounts AS account ON account.account_id = hits.account_id
         WHERE hit_deep_analysis.version = (
               SELECT MAX(version) FROM hit_deep_analysis AS d2 WHERE d2.hit_id = hit_deep_analysis.hit_id
           )
           AND NOT EXISTS (
               SELECT 1 FROM topic_candidates WHERE topic_candidates.source_analysis_id = hit_deep_analysis.analysis_id
           )
         ORDER BY hit_deep_analysis.created_at
         LIMIT ?
        """,
        (TOP_COMMENTS_DELIMITER, limit),
    ).fetchall()


def _comment_evidence_items(top_comments_text: str | None) -> list[str]:
    """Splits the GROUP_CONCAT'd top-comments column back into individual
    evidence items, each labeled so the model (and any human reading the
    stored input payload later) can tell this is real audience reaction, not
    the analysis-derived pattern items."""
    if not top_comments_text:
        return []
    comments = [c for c in top_comments_text.split(TOP_COMMENTS_DELIMITER) if c.strip()]
    return [f"热门评论:{c}" for c in comments]


def _hotspot_evidence_items(conn: sqlite3.Connection, *, domain_label: str, window_days: int = HOTSPOT_EVIDENCE_WINDOW_DAYS) -> list[str]:
    """2026-07-13(D1, BR-TOPIC-006):真实登记的热点(A6 的 hotspot_events)自动
    纳入证据,不需要人工逐条关联(用户明确选择)。只取这个领域下、最近
    window_days 天内登记的热点,不做跨领域混用。不截断(匹配这次会话对字符
    上限的决定)。"""
    if domain_label not in ALLOWED_DOMAIN_LABELS:
        return []
    cutoff = (datetime.now(timezone.utc) - timedelta(days=window_days)).isoformat()
    rows = conn.execute(
        "SELECT raw_text FROM hotspot_events WHERE domain_label = ? AND created_at >= ? ORDER BY created_at DESC",
        (domain_label, cutoff),
    ).fetchall()
    return [f"当下热点:{row['raw_text']}" for row in rows]


def assemble_source_to_topic_input(conn: sqlite3.Connection, analysis_row: sqlite3.Row, *, run_id: str) -> dict[str, Any]:
    """A real hit_deep_analysis row (joined with its hit's top real comments,
    see select_analyses_pending_topic()) in, source_to_topic's exact public
    input contract out. No internal id beyond the Skill's own request_id/
    source_id fields, no raw path. No longer a pure function as of D1
    (2026-07-13): needs `conn` to pull this domain's recent real hotspot
    events (see module docstring).

    2026-07-10: source_evidence_items mixes analysis patterns (对标爆款) and
    real top comments (评论区). 2026-07-13(D1): 当下热点(hotspot_events) added
    as a third source. 研究缺口 remains unwired -- the concept itself is not
    yet clearly defined against any authoritative source (see module
    docstring's 2026-07-13 note), not guessed at here."""
    domain_label = analysis_row["account_domain_label"]
    if domain_label not in ALLOWED_DOMAIN_LABELS:
        domain_label = "unknown"
    account_name = (analysis_row["account_name"] or "").strip()
    hit_title = (analysis_row["hit_title"] or "").strip()
    source_content = f"账号「{account_name}」的爆款视频「{hit_title}」经深度分析后提炼的选题/开头/结构手法。"
    evidence_items = [
        f"选题手法:{analysis_row['topic_pattern']}",
        f"开头手法:{analysis_row['hook_pattern']}",
        f"结构手法:{analysis_row['structure_pattern']}",
    ]
    try:
        top_comments_text = analysis_row["top_comments_text"]
    except (IndexError, KeyError):
        top_comments_text = None
    evidence_items.extend(_comment_evidence_items(top_comments_text))
    evidence_items.extend(_hotspot_evidence_items(conn, domain_label=domain_label))
    if len(evidence_items) > SOURCE_EVIDENCE_ITEMS_MAX:
        # 2026-07-13(D1): hotspot evidence items now also count toward this
        # total, so this is no longer purely "should be mathematically
        # impossible" the way it was when only comments could push evidence
        # count up -- a domain with many real hotspot registrations in the
        # window could genuinely hit this. Failing loudly here (rather than
        # silently truncating) is still the right behavior either way.
        raise ValueError(
            f"analysis_id={analysis_row['analysis_id']!r} has {len(evidence_items)} evidence items, "
            f"exceeding SOURCE_EVIDENCE_ITEMS_MAX={SOURCE_EVIDENCE_ITEMS_MAX} -- check whether "
            "BR-COLLECT-005/006's real top_comments limit changed, or whether this domain has an "
            "unusually large number of real hotspot_events registered within the window"
        )
    return {
        "request_id": f"source_to_topic_{analysis_row['analysis_id']}",
        "correlation_id": run_id,
        "source_id": analysis_row["analysis_id"],
        "source_content": source_content,
        "source_evidence_items": evidence_items,
        "domain_label": domain_label,
        "relation_summary": NO_RELATION_JUDGEMENT_YET,
        "schema_version": "source_to_topic.input.v1",
    }


def _persist_topic(conn: sqlite3.Connection, analysis_id: str, input_payload: dict[str, Any], output: dict[str, Any], *, model_name: str, run_id: str) -> str:
    version = (conn.execute("SELECT COALESCE(MAX(version), 0) FROM topic_candidates WHERE source_analysis_id=?", (analysis_id,)).fetchone()[0]) + 1
    topic_id = f"{analysis_id}_topic_v{version}"
    conn.execute(
        """
        INSERT INTO topic_candidates(
            topic_id, source_analysis_id, version, request_id, correlation_id,
            topic_status, candidate_topic, topic_angle, supporting_evidence,
            source_constraints, no_result_reason, confidence, model_name, run_id
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """,
        (
            topic_id, analysis_id, version, input_payload["request_id"], input_payload["correlation_id"],
            output["topic_status"], output["candidate_topic"], output["topic_angle"],
            json.dumps(output["supporting_evidence"], ensure_ascii=False),
            json.dumps(output["source_constraints"], ensure_ascii=False),
            output["no_result_reason"], output["confidence"], model_name, run_id,
        ),
    )
    conn.commit()
    return topic_id


DOMAIN_CONSTRAINT_NOTE = "账号领域标签不在已知领域枚举内,系统自动要求人工复核(BR-TOPIC-001 领域约束)"


def _apply_domain_constraint(input_payload: dict[str, Any], output: dict[str, Any]) -> dict[str, Any]:
    """2026-07-13(D2, BR-TOPIC-001):唯一的确定性领域约束检查——不做"内容是否
    真的符合该领域调性"这类语义判断(那需要LLM打分,BR-TOPIC-001明确禁止评分
    排序)。只查一件事:候选选题所属账号的 domain_label 是否落在
    ALLOWED_DOMAIN_LABELS 之外(assemble_source_to_topic_input 里已经把这种
    情况标成 "unknown")。skill 自己判定 no_result/needs_review 的情况不覆盖,
    只在 skill 判定 generated 时才强制拉回 needs_review,不能悄悄流到下一步。"""
    if input_payload["domain_label"] != "unknown" or output["topic_status"] != "generated":
        return output
    output = dict(output)
    output["topic_status"] = "needs_review"
    output["source_constraints"] = list(output.get("source_constraints") or []) + [DOMAIN_CONSTRAINT_NOTE]
    return output


def generate_one_topic(conn: sqlite3.Connection, harness: FormalBusinessSkillHarness, analysis_row: sqlite3.Row, *, run_id: str, model_name: str) -> dict[str, Any]:
    input_payload = assemble_source_to_topic_input(conn, analysis_row, run_id=run_id)
    created = harness.api.create_formal_skill_job(input_payload, max_attempts=1)
    step = harness.worker.run_once()
    if step.status != "succeeded":
        return {"analysis_id": analysis_row["analysis_id"], "status": "failed", "reason": step.reason or step.status}
    result = harness.api.get_result(created.job_id)
    if result is None:
        return {"analysis_id": analysis_row["analysis_id"], "status": "failed", "reason": "no result materialized"}
    output = _apply_domain_constraint(input_payload, result["output"])
    topic_id = _persist_topic(conn, analysis_row["analysis_id"], input_payload, output, model_name=model_name, run_id=run_id)
    return {"analysis_id": analysis_row["analysis_id"], "status": "completed", "topic_id": topic_id, "topic_status": output["topic_status"]}


def _load_recent_candidates_for_dedup(
    conn: sqlite3.Connection,
    *,
    account_id: str,
    exclude_topic_id: str,
    window_days: int = DEDUP_CANDIDATE_WINDOW_DAYS,
    limit: int = DEDUP_CANDIDATE_COMPARE_MAX,
) -> list[sqlite3.Row]:
    """2026-07-13(D3, BR-TOPIC-001):同一账号、最近 window_days 天内的候选选题
    (排除自己),按时间倒序最多 limit 条,供去重比对使用。同一账号的
    domain_label 在注册时就固定了,不需要单独按领域过滤。"""
    cutoff = (datetime.now(timezone.utc) - timedelta(days=window_days)).isoformat()
    return conn.execute(
        """
        SELECT topic_candidates.topic_id AS topic_id, topic_candidates.candidate_topic AS candidate_topic,
               topic_candidates.topic_angle AS topic_angle, topic_candidates.supporting_evidence AS supporting_evidence
          FROM topic_candidates
          JOIN hit_deep_analysis ON hit_deep_analysis.analysis_id = topic_candidates.source_analysis_id
          JOIN hits ON hits.hit_id = hit_deep_analysis.hit_id
         WHERE hits.account_id = ?
           AND topic_candidates.topic_id != ?
           AND topic_candidates.created_at >= ?
         ORDER BY topic_candidates.created_at DESC
         LIMIT ?
        """,
        (account_id, exclude_topic_id, cutoff, limit),
    ).fetchall()


def check_topic_candidate_duplicate(
    conn: sqlite3.Connection, relation_harness: FormalBusinessSkillHarness, *, topic_id: str
) -> dict[str, Any] | None:
    """2026-07-13(D3, BR-TOPIC-001):去重/冷却。跟同一账号最近
    DEDUP_CANDIDATE_WINDOW_DAYS 天内的候选选题逐条比对(最多
    DEDUP_CANDIDATE_COMPARE_MAX 条),第一次真正用 content_relation_judge 吃
    真实数据。命中 DUPLICATE_RELATION_TYPES 任一种就把这条候选标成
    needs_review(不自动拒绝,BR-TOPIC-004 要求人工确认),并在
    source_constraints 里记录碰撞的 topic_id + relation_type;命中即停止,不用
    跟每一条都判完。返回 None 表示没发现重复,返回 dict 表示命中。"""
    row = conn.execute(
        """
        SELECT topic_candidates.*, hits.account_id AS account_id, account.domain_label AS account_domain_label
          FROM topic_candidates
          JOIN hit_deep_analysis ON hit_deep_analysis.analysis_id = topic_candidates.source_analysis_id
          JOIN hits ON hits.hit_id = hit_deep_analysis.hit_id
          JOIN competitor_accounts AS account ON account.account_id = hits.account_id
         WHERE topic_candidates.topic_id = ?
        """,
        (topic_id,),
    ).fetchone()
    if row is None:
        raise ValueError(f"topic_id {topic_id!r} does not exist")

    candidates = _load_recent_candidates_for_dedup(conn, account_id=row["account_id"], exclude_topic_id=topic_id)
    if not candidates:
        return None

    domain_label = row["account_domain_label"]
    if domain_label not in ALLOWED_DOMAIN_LABELS:
        domain_label = "unknown"
    left_content = f"{row['candidate_topic']}。{row['topic_angle']}"
    # content_relation_judge's own contract requires real evidence for any
    # "concrete" relation type (same_item/equivalent/contains/... all need
    # evidence_from_left/right, per evidence_requirements.content_relations_
    # require_left_evidence) -- a real candidate's supporting_evidence can
    # legitimately be empty, so fall back to the candidate's own
    # topic+angle text as its own evidence rather than sending an empty
    # array the Skill can never ground a concrete relation in.
    left_evidence = json.loads(row["supporting_evidence"] or "[]")[:8] or [left_content]

    for existing in candidates:
        right_content = f"{existing['candidate_topic']}。{existing['topic_angle']}"
        right_evidence = json.loads(existing["supporting_evidence"] or "[]")[:8] or [right_content]
        input_payload = {
            "request_id": f"content_relation_judge_{topic_id}_vs_{existing['topic_id']}",
            "correlation_id": topic_id,
            "left_content": left_content,
            "right_content": right_content,
            "left_evidence_items": left_evidence,
            "right_evidence_items": right_evidence,
            "domain_context": domain_label,
            "relation_scope": "content_object",
            "schema_version": "content_relation_judge.input.v1",
        }
        created = relation_harness.api.create_formal_skill_job(input_payload, max_attempts=1)
        step = relation_harness.worker.run_once()
        if step.status != "succeeded":
            continue
        result = relation_harness.api.get_result(created.job_id)
        if result is None:
            continue
        relation_type = result["output"]["relation_type"]
        if relation_type in DUPLICATE_RELATION_TYPES:
            note = f"疑似与已有选题 {existing['topic_id']} 构成 {relation_type} 关系(2026-07-13起自动去重判定)"
            new_constraints = json.loads(row["source_constraints"] or "[]") + [note]
            conn.execute(
                "UPDATE topic_candidates SET topic_status='needs_review', source_constraints=? WHERE topic_id=?",
                (json.dumps(new_constraints, ensure_ascii=False), topic_id),
            )
            conn.commit()
            return {"topic_id": topic_id, "duplicate_of": existing["topic_id"], "relation_type": relation_type}
    return None


def run_source_to_topic(
    conn: sqlite3.Connection,
    *,
    limit: int,
    harness: FormalBusinessSkillHarness,
    model_name: str,
    relation_harness: FormalBusinessSkillHarness | None = None,
) -> dict[str, Any]:
    validate_source_to_topic_execution_contract()
    run_id = "source_to_topic_" + datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    pending = select_analyses_pending_topic(conn, limit=limit)
    results = [generate_one_topic(conn, harness, row, run_id=run_id, model_name=model_name) for row in pending]
    if relation_harness is not None:
        for r in results:
            if r["status"] == "completed":
                duplicate = check_topic_candidate_duplicate(conn, relation_harness, topic_id=r["topic_id"])
                if duplicate is not None:
                    r["topic_status"] = "needs_review"
                    r["duplicate_of"] = duplicate["duplicate_of"]
                    r["relation_type"] = duplicate["relation_type"]
    return {
        "status": "succeeded",
        "run_id": run_id,
        "attempted": len(results),
        "completed": sum(1 for r in results if r["status"] == "completed"),
        "failed": sum(1 for r in results if r["status"] == "failed"),
        "results": results,
    }


def build_real_harness(*, env_path: Path | None = None) -> tuple[FormalBusinessSkillHarness, str]:
    """Same credential gap as run_sample_deep_analyze.py's build_real_harness():
    HERMES_BUSINESS_MODEL_* only exists in .env.live-gates, not the real .env,
    as of this writing. Raises RuntimeError via _load_env_value if absent."""
    token = _load_env_value("HERMES_BUSINESS_MODEL_TOKEN", env_path=env_path)
    base_url = _load_env_value("HERMES_BUSINESS_MODEL_BASE_URL", env_path=env_path)
    model_name = _load_env_value("HERMES_BUSINESS_MODEL_NAME", env_path=env_path)
    model_class = _load_env_value("HERMES_BUSINESS_MODEL_CLASS", env_path=env_path)
    if model_class.strip().lower() != "mimo":
        raise RuntimeError("HERMES_BUSINESS_MODEL_CLASS must be mimo for the production business route")

    adapter = HermesModelProviderAdapter(HermesModelProviderConfig(api_key=token, base_url=base_url, model=model_name, timeout_seconds=90, max_retries=0))
    route = ModelRoute(
        route_name="business.source_to_topic",
        provider_name=adapter.provider_name,
        model_name=model_name,
        config_version="source_to_topic.production.v1",
        config_hash=content_hash({"route": "business.source_to_topic", "provider": "hermes", "model": model_name}),
        parameters={"temperature": 0, "response_format": {"type": "json_object"}},
        timeout_ms=60000,
    )
    harness = make_source_to_topic_harness(provider=adapter, route=route)  # type: ignore[arg-type]
    return harness, model_name


def build_real_relation_harness(*, env_path: Path | None = None) -> FormalBusinessSkillHarness:
    """2026-07-13(D3):同一套 HERMES_BUSINESS_MODEL_* 凭据,给 content_relation_judge
    单独建一个 harness(不同 route_name,不能跟 source_to_topic 共用同一个
    ModelRoute)。这是 content_relation_judge 第一次真正接上真实数据。"""
    token = _load_env_value("HERMES_BUSINESS_MODEL_TOKEN", env_path=env_path)
    base_url = _load_env_value("HERMES_BUSINESS_MODEL_BASE_URL", env_path=env_path)
    model_name = _load_env_value("HERMES_BUSINESS_MODEL_NAME", env_path=env_path)
    model_class = _load_env_value("HERMES_BUSINESS_MODEL_CLASS", env_path=env_path)
    if model_class.strip().lower() != "mimo":
        raise RuntimeError("HERMES_BUSINESS_MODEL_CLASS must be mimo for the production business route")

    adapter = HermesModelProviderAdapter(HermesModelProviderConfig(api_key=token, base_url=base_url, model=model_name, timeout_seconds=90, max_retries=0))
    route = ModelRoute(
        route_name="business.content_relation_judgement",
        provider_name=adapter.provider_name,
        model_name=model_name,
        config_version="content_relation_judge.production.v1",
        config_hash=content_hash({"route": "business.content_relation_judgement", "provider": "hermes", "model": model_name}),
        parameters={"temperature": 0, "response_format": {"type": "json_object"}},
        timeout_ms=30000,
    )
    return make_content_relation_judge_harness(provider=adapter, route=route)  # type: ignore[arg-type]


def main(argv: list[str] | None = None) -> int:
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8")
    parser = argparse.ArgumentParser(description="Turn pending sample_deep_analyze output into candidate topics via runtime_skills/source_to_topic.")
    parser.add_argument("--limit", type=int, default=1)
    parser.add_argument("--db", default=str(DEFAULT_DB))
    parser.add_argument("--json", action="store_true")
    parser.add_argument(
        "--env-file",
        default=None,
        help="Override which .env-style file supplies HERMES_BUSINESS_MODEL_* credentials "
        "(defaults to the real .env). Pass .env.live-gates for a one-off test run without "
        "permanently copying those credentials into .env.",
    )
    args = parser.parse_args(argv)

    db_path = _safe_db_path(Path(args.db))
    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    env_path = Path(args.env_file) if args.env_file else None
    harness, model_name = build_real_harness(env_path=env_path)
    relation_harness = build_real_relation_harness(env_path=env_path)
    try:
        install_schema(conn)
        install_domain_search_schema(conn)
        report = run_source_to_topic(
            conn, limit=args.limit, harness=harness, model_name=model_name, relation_harness=relation_harness
        )
    finally:
        conn.close()
        harness.close()
        relation_harness.close()

    if args.json:
        print(json.dumps(report, ensure_ascii=False, indent=2))
    else:
        import yaml as _yaml
        print(_yaml.safe_dump(report, allow_unicode=True, sort_keys=False))
    return 0 if report["status"] == "succeeded" else 2


if __name__ == "__main__":
    raise SystemExit(main())
