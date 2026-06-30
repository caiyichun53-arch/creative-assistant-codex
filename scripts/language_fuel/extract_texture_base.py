"""Validate extraction of a non-template human language texture base.

This is a sidecar to the existing language_fuel_insights flow.

Purpose:
- Preserve one hit/video comment context before abstraction.
- Extract expression judgments, context conditions, and failure boundaries.
- Merge only cross-context recurring observations into candidate base items.
- Diagnose short scripts with the candidate base without injecting it into the
  normal writing flow.

It does not crawl platforms and it does not write to language_fuel_insights.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import re
import sys
from datetime import datetime
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "scripts" / "collect"))
sys.path.insert(0, str(ROOT / "scripts" / "llm"))
sys.stdout.reconfigure(encoding="utf-8", errors="replace")

from call import SessionLimitError, call_llm  # noqa: E402
from common import connect, safe_title  # noqa: E402

OUT_DIR = ROOT / "data" / "language_fuel" / "texture_base"
LLM_NODE = "language_fuel_extract"

LOCAL_PROMPT = """你是中文创作系统里的“真人语言肌理提炼员”。

任务：从同一个爆款视频/同一个评论区中，提炼“表达判断”，不要提炼句式模板、爆款技巧、可仿写原句。

严格边界：
- 不要总结视频内容。
- 不要输出“多用短句/多自嘲/开头反问”这类空泛技巧。
- 不要保留、改写、复刻网友原句；证据只写评论编号。
- 只记录“为什么这种表达在这个语境里像真人”，以及“机械套用会怎样失败”。
- 如果某个观察只在本视频成立，要写进 only_local_signals，不要装成通用规律。

视频/评论区信息：
hit_id: {hit_id}
标题: {title}
领域: {domain}
对标账号: {competitor}
爆款强度: 超额 {excess_ratio}x

评论清单：
{comments}

请严格输出 JSON，不要 markdown：
{{
  "group_summary": {{
    "emotional_ground": "这组评论共同的情绪底色，一两句",
    "common_expression_actions": ["这组评论常见的表达动作，不是句式"],
    "local_only": ["只在这个视频/话题下成立的表达现象"]
  }},
  "observations": [
    {{
      "expression_tendency": "真人表达倾向：描述判断方式，不给公式",
      "context_conditions": "它为什么在这个评论区成立：语境、对象、情绪、平台氛围",
      "not_for": "不适用场景",
      "failure_boundary": "一旦机械套用，会怎么变油、变模板、变AI",
      "mechanical_misuse": "失败版本长什么样：只描述，不写可用成稿句",
      "diagnostic_question": "审稿时用来判断一段文字是否像真人的问题",
      "only_local_signals": "哪些信号说明它可能只在本语境成立；若可跨语境，写空字符串",
      "evidence_indices": [1, 2],
      "strength": 1
    }}
  ]
}}
"""

CROSS_PROMPT = """你是中文创作系统里的“真人语言肌理归纳员”。

任务：把多个视频/评论区已经提炼出的“语境内观察”合并，找出跨语境仍成立的候选底座。

严格边界：
- 不要把单个视频的梗、口头禅、固定句式升级为底座。
- 不要输出写作公式。
- 每条候选必须至少由 {min_sources} 个不同 source_id 支撑。
- 输出的是表达判断，不是成稿语言。
- 必须写清楚失败边界；写不清失败边界的，不要输出。

候选观察列表：
{observations}

请严格输出 JSON，不要 markdown：
{{
  "base_candidates": [
    {{
      "expression_tendency": "跨语境表达倾向：描述判断方式，不给公式",
      "cross_context_reason": "为什么不是单个视频现象",
      "context_conditions": "适用语境",
      "not_for": "不适用场景",
      "failure_boundary": "机械套用的失败边界",
      "mechanical_misuse": "失败版本长什么样：只描述，不写可用成稿句",
      "diagnostic_question": "审稿诊断问题",
      "supported_source_ids": ["44", "53"],
      "strength": 1
    }}
  ]
}}
"""

DIAGNOSE_PROMPT = """你是中文创作系统里的“真人语言肌理诊断员”。

任务：用候选底座诊断一段口播稿哪里像 AI、哪里太满、太顺、太像总结或模板。
不要整篇重写，只做局部诊断和少量示范性改写。

候选底座：
{base_items}

待诊断文本：
{text}

请严格输出 JSON，不要 markdown：
{{
  "diagnosis": [
    {{
      "problem": "问题描述",
      "basis": "对应哪条底座判断",
      "why_not_human": "为什么不像真人",
      "local_rewrite": "只改这一处的示范，不要整篇重写"
    }}
  ],
  "overall_judgment": "这套底座是否帮得上忙：能/不能/不确定，并说明原因"
}}
"""

REWRITE_PROMPT = """你是中文创作系统里的“真人语言肌理改写员”。

任务：用候选底座把一段明显偏 AI 的口播稿改成更像真人说话的版本。

严格边界：
- 不要加新事实，不要编数据，不要扩大结论。
- 不要把稿子改成段子，不要为了网感硬加梗。
- 不要照搬候选底座里的表达；底座只用于判断哪里太满、太顺、太像总结。
- 保留原文的核心信息，但允许调整进入方式、判断姿态、停顿、条件、生活位置和收束。
- 输出必须包含原文问题诊断、完整改写稿、改写说明、风险自检。

候选底座：
{base_items}

原始 AI 稿：
{text}

请严格输出 JSON，不要 markdown：
{{
  "original_text": "原始 AI 稿原文",
  "diagnosis": [
    {{
      "problem": "问题描述",
      "basis": "对应哪条底座判断",
      "why_not_human": "为什么不像真人"
    }}
  ],
  "rewritten_text": "完整改写稿",
  "change_notes": [
    "改写做了什么，不写方法论空话"
  ],
  "risk_check": {{
    "added_unverified_facts": false,
    "over_internet_slang": false,
    "mechanical_formula": false,
    "notes": "如有风险写清楚"
  }}
}}
"""


def _hash(*parts: str) -> str:
    text = "\n".join(parts)
    return hashlib.sha1(text.encode("utf-8")).hexdigest()[:16]


def _strip_code_fence(text: str) -> str:
    text = text.strip()
    if text.startswith("```"):
        text = re.sub(r"^```(?:json)?", "", text).strip()
        text = re.sub(r"```$", "", text).strip()
    return text


def _loads_json(text: str) -> dict:
    return json.loads(_strip_code_fence(text))


def _clean_text(text: str | None, max_chars: int = 500) -> str:
    text = re.sub(r"\s+", " ", text or "").strip()
    return text[:max_chars]


def _comment_block(rows) -> str:
    lines = []
    for i, row in enumerate(rows, 1):
        like = row["like_count"] or 0
        reply = row["sub_comment_count"] or 0
        text = _clean_text(row["content"], 220)
        lines.append(f"{i}. [{like}赞/{reply}回复] {text}")
    return "\n".join(lines)


def _pending_hits(conn, hit_id: int | None, limit_hits: int, force: bool):
    base = (
        "SELECT h.*, c.domain AS domain, c.name AS competitor "
        "FROM hits h JOIN competitor_accounts c ON c.id=h.competitor_id "
        "WHERE EXISTS (SELECT 1 FROM hit_comments hc WHERE hc.hit_id=h.id)"
    )
    params: list[object] = []
    if hit_id:
        base += " AND h.id=?"
        params.append(hit_id)
    elif not force:
        base += (
            " AND NOT EXISTS (SELECT 1 FROM language_texture_observations o "
            "WHERE o.scope='local_context' AND o.source_type='hit_comments' "
            "AND o.source_id=CAST(h.id AS TEXT))"
        )
    base += " ORDER BY h.excess_ratio DESC, h.id DESC LIMIT ?"
    params.append(limit_hits)
    return conn.execute(base, params).fetchall()


def _comments_for_hit(conn, hit_id: int, limit: int):
    return conn.execute(
        """SELECT * FROM hit_comments WHERE hit_id=?
           ORDER BY COALESCE(like_count,0) DESC, COALESCE(sub_comment_count,0) DESC
           LIMIT ?""",
        (hit_id, limit),
    ).fetchall()


def _int_strength(value) -> int:
    try:
        return max(1, min(5, int(value or 3)))
    except (TypeError, ValueError):
        return 3


def _parse_local(raw: str) -> tuple[dict, list[dict]]:
    data = _loads_json(raw)
    summary = data.get("group_summary") or {}
    observations = []
    for item in data.get("observations", []):
        if not isinstance(item, dict):
            continue
        tendency = _clean_text(item.get("expression_tendency"), 800)
        context = _clean_text(item.get("context_conditions"), 800)
        failure = _clean_text(item.get("failure_boundary"), 800)
        misuse = _clean_text(item.get("mechanical_misuse"), 800)
        if not tendency or not context or not failure or not misuse:
            continue
        evidence = item.get("evidence_indices") or []
        if not isinstance(evidence, list):
            evidence = []
        observations.append({
            "expression_tendency": tendency,
            "context_conditions": context,
            "not_for": _clean_text(item.get("not_for"), 500),
            "failure_boundary": failure,
            "mechanical_misuse": misuse,
            "diagnostic_question": _clean_text(item.get("diagnostic_question"), 500),
            "only_local_signals": _clean_text(item.get("only_local_signals"), 500),
            "evidence_indices": [int(x) for x in evidence if str(x).isdigit()][:8],
            "strength": _int_strength(item.get("strength")),
        })
    return summary, observations


def _parse_cross(raw: str, min_sources: int) -> list[dict]:
    data = _loads_json(raw)
    out = []
    for item in data.get("base_candidates", []):
        if not isinstance(item, dict):
            continue
        source_ids = [str(x) for x in item.get("supported_source_ids", []) if str(x).strip()]
        if len(set(source_ids)) < min_sources:
            continue
        tendency = _clean_text(item.get("expression_tendency"), 800)
        context = _clean_text(item.get("context_conditions"), 800)
        failure = _clean_text(item.get("failure_boundary"), 800)
        misuse = _clean_text(item.get("mechanical_misuse"), 800)
        if not tendency or not context or not failure or not misuse:
            continue
        out.append({
            "expression_tendency": tendency,
            "context_conditions": context,
            "not_for": _clean_text(item.get("not_for"), 500),
            "failure_boundary": failure,
            "mechanical_misuse": misuse,
            "diagnostic_question": _clean_text(item.get("diagnostic_question"), 500),
            "cross_context_reason": _clean_text(item.get("cross_context_reason"), 800),
            "supported_source_ids": sorted(set(source_ids)),
            "strength": _int_strength(item.get("strength")),
        })
    return out


def _store_observation(conn, *, scope: str, source_id: str, platform: str, domain: str,
                       title: str | None, item: dict, source_group_ids: list[str],
                       evidence: dict, notes: dict, status: str) -> int:
    content_hash = _hash(
        scope,
        item["expression_tendency"],
        item["context_conditions"],
        item["failure_boundary"],
        json.dumps(source_group_ids, ensure_ascii=False),
    )
    cur = conn.execute(
        """INSERT OR IGNORE INTO language_texture_observations
           (scope, source_type, source_id, platform, domain, title,
            expression_tendency, context_conditions, not_for,
            failure_boundary, mechanical_misuse, diagnostic_question,
            only_local_signals, source_group_ids, evidence_json, notes_json,
            strength, status, content_hash)
           VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
        (
            scope,
            "hit_comments",
            source_id,
            platform,
            domain,
            title,
            item["expression_tendency"],
            item["context_conditions"],
            item.get("not_for") or "",
            item["failure_boundary"],
            item["mechanical_misuse"],
            item.get("diagnostic_question") or "",
            item.get("only_local_signals") or "",
            json.dumps(source_group_ids, ensure_ascii=False),
            json.dumps(evidence, ensure_ascii=False),
            json.dumps(notes, ensure_ascii=False),
            item.get("strength") or 3,
            status,
            content_hash,
        ),
    )
    return cur.rowcount


def _write_payload(path: Path, payload: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")


def extract_local(hit_id: int | None, limit_hits: int, comments_per_hit: int,
                  dry_run: bool, force: bool) -> None:
    conn = connect()
    try:
        if force and hit_id and not dry_run:
            with conn:
                conn.execute(
                    "DELETE FROM language_texture_observations "
                    "WHERE scope='local_context' AND source_type='hit_comments' AND source_id=?",
                    (str(hit_id),),
                )
        hits = _pending_hits(conn, hit_id, limit_hits, force=force)
        if not hits:
            print("no pending hit comment groups for texture extraction")
            return
        for hit in hits:
            comments = _comments_for_hit(conn, hit["id"], comments_per_hit)
            if not comments:
                continue
            prompt = LOCAL_PROMPT.format(
                hit_id=hit["id"],
                title=hit["title"] or "",
                domain=hit["domain"] or "",
                competitor=hit["competitor"] or "",
                excess_ratio=hit["excess_ratio"] or "",
                comments=_comment_block(comments),
            )
            raw = call_llm(LLM_NODE, prompt, timeout=600)
            summary, observations = _parse_local(raw)
            payload = {
                "scope": "local_context",
                "source_type": "hit_comments",
                "source_id": hit["id"],
                "platform": "douyin",
                "domain": hit["domain"],
                "title": hit["title"],
                "group_summary": summary,
                "observations": observations,
            }
            if dry_run:
                print(json.dumps(payload, ensure_ascii=False, indent=2))
                continue
            title = safe_title(hit["title"], hit["id"])
            _write_payload(
                OUT_DIR / "local_context" / str(hit["domain"] or "general") / f"{hit['id']}_{title}.json",
                payload,
            )
            written = 0
            with conn:
                for item in observations:
                    status = "local_only" if item.get("only_local_signals") else "candidate"
                    written += _store_observation(
                        conn,
                        scope="local_context",
                        source_id=str(hit["id"]),
                        platform="douyin",
                        domain=hit["domain"] or "general",
                        title=hit["title"],
                        item=item,
                        source_group_ids=[str(hit["id"])],
                        evidence={"evidence_indices": item.get("evidence_indices", [])},
                        notes={"group_summary": summary},
                        status=status,
                    )
            print(f"hit {hit['id']}: extracted {len(observations)}, inserted {written}")
    except SessionLimitError:
        raise
    finally:
        conn.close()


def _local_candidates(conn, domain: str | None, limit: int):
    # Include local_only rows at merge time. A single source cannot promote them,
    # but repeated tendencies across different sources may prove the observation
    # was not actually local-only.
    where = "WHERE scope='local_context' AND status IN ('candidate','local_only')"
    params: list[object] = []
    if domain:
        where += " AND domain=?"
        params.append(domain)
    sql = (
        "SELECT * FROM language_texture_observations "
        f"{where} ORDER BY strength DESC, created_at DESC LIMIT ?"
    )
    params.append(limit)
    return conn.execute(sql, params).fetchall()


def _format_observations(rows) -> str:
    blocks = []
    for i, row in enumerate(rows, 1):
        blocks.append(
            f"{i}. source_id={row['source_id']} | domain={row['domain']} | status={row['status']} | title={_clean_text(row['title'], 80)}\n"
            f"   expression_tendency: {row['expression_tendency']}\n"
            f"   context_conditions: {row['context_conditions']}\n"
            f"   not_for: {row['not_for'] or ''}\n"
            f"   failure_boundary: {row['failure_boundary']}\n"
            f"   mechanical_misuse: {row['mechanical_misuse']}\n"
            f"   diagnostic_question: {row['diagnostic_question'] or ''}\n"
            f"   only_local_signals: {row['only_local_signals'] or ''}"
        )
    return "\n\n".join(blocks)


def merge_cross_context(domain: str | None, limit_observations: int, min_sources: int,
                        dry_run: bool) -> None:
    conn = connect()
    try:
        rows = _local_candidates(conn, domain, limit_observations)
        source_count = len({str(r["source_id"]) for r in rows})
        if source_count < min_sources:
            raise SystemExit(
                f"need at least {min_sources} source groups, got {source_count}; run local extraction first"
            )
        prompt = CROSS_PROMPT.format(
            min_sources=min_sources,
            observations=_format_observations(rows),
        )
        raw = call_llm(LLM_NODE, prompt, timeout=600)
        candidates = _parse_cross(raw, min_sources=min_sources)
        merge_id = f"merge:{datetime.now():%Y%m%d%H%M%S}"
        payload = {
            "scope": "cross_context",
            "source_type": "hit_comments",
            "source_id": merge_id,
            "domain": domain or "mixed",
            "min_sources": min_sources,
            "base_candidates": candidates,
        }
        if dry_run:
            print(json.dumps(payload, ensure_ascii=False, indent=2))
            return
        _write_payload(OUT_DIR / "cross_context" / f"{merge_id.replace(':', '_')}.json", payload)
        written = 0
        with conn:
            for item in candidates:
                written += _store_observation(
                    conn,
                    scope="cross_context",
                    source_id=merge_id,
                    platform="douyin",
                    domain=domain or "mixed",
                    title="human language texture base candidate",
                    item=item,
                    source_group_ids=item["supported_source_ids"],
                    evidence={"source_observation_ids": [r["id"] for r in rows]},
                    notes={"cross_context_reason": item.get("cross_context_reason", "")},
                    status="candidate",
                )
        print(f"{merge_id}: merged {len(candidates)}, inserted {written}")
    except SessionLimitError:
        raise
    finally:
        conn.close()


def _base_items(conn, domain: str | None, limit: int):
    where = "WHERE scope='cross_context' AND status IN ('candidate','promoted')"
    params: list[object] = []
    if domain:
        where += " AND domain IN (?, 'mixed')"
        params.append(domain)
    sql = (
        "SELECT * FROM language_texture_observations "
        f"{where} ORDER BY strength DESC, created_at DESC LIMIT ?"
    )
    params.append(limit)
    return conn.execute(sql, params).fetchall()


def diagnose_text(text: str, domain: str | None, limit: int) -> None:
    conn = connect()
    try:
        rows = _base_items(conn, domain, limit)
        if not rows:
            raise SystemExit("no cross-context texture candidates found; run merge first")
        base_items = "\n".join(
            f"- {r['expression_tendency']} | 适用: {r['context_conditions']} | 失败边界: {r['failure_boundary']}"
            for r in rows
        )
        raw = call_llm(
            LLM_NODE,
            DIAGNOSE_PROMPT.format(base_items=base_items, text=text),
            timeout=600,
        )
        print(_strip_code_fence(raw))
    finally:
        conn.close()


def rewrite_text(text: str, domain: str | None, limit: int) -> None:
    conn = connect()
    try:
        rows = _base_items(conn, domain, limit)
        if not rows:
            raise SystemExit("no cross-context texture candidates found; run merge first")
        base_items = "\n".join(
            f"- {r['expression_tendency']} | 适用: {r['context_conditions']} | 失败边界: {r['failure_boundary']}"
            for r in rows
        )
        raw = call_llm(
            LLM_NODE,
            REWRITE_PROMPT.format(base_items=base_items, text=text),
            timeout=600,
        )
        print(_strip_code_fence(raw))
    finally:
        conn.close()


def main() -> None:
    parser = argparse.ArgumentParser()
    sub = parser.add_subparsers(dest="command", required=True)

    p_local = sub.add_parser("local", help="extract context-preserving observations per hit")
    p_local.add_argument("--hit", type=int)
    p_local.add_argument("--limit-hits", type=int, default=5)
    p_local.add_argument("--comments-per-hit", type=int, default=80)
    p_local.add_argument("--dry-run", action="store_true")
    p_local.add_argument("--force", action="store_true")

    p_merge = sub.add_parser("merge", help="merge local observations into cross-context base candidates")
    p_merge.add_argument("--domain")
    p_merge.add_argument("--limit-observations", type=int, default=80)
    p_merge.add_argument("--min-sources", type=int, default=2)
    p_merge.add_argument("--dry-run", action="store_true")

    p_diag = sub.add_parser("diagnose", help="diagnose a short script with cross-context candidates")
    p_diag.add_argument("--domain")
    p_diag.add_argument("--limit", type=int, default=8)
    p_diag.add_argument("--text")
    p_diag.add_argument("--text-file")

    p_rewrite = sub.add_parser("rewrite", help="rewrite a short script with cross-context candidates")
    p_rewrite.add_argument("--domain")
    p_rewrite.add_argument("--limit", type=int, default=8)
    p_rewrite.add_argument("--text")
    p_rewrite.add_argument("--text-file")

    args = parser.parse_args()
    if args.command == "local":
        extract_local(args.hit, args.limit_hits, args.comments_per_hit, args.dry_run, args.force)
    elif args.command == "merge":
        merge_cross_context(args.domain, args.limit_observations, args.min_sources, args.dry_run)
    elif args.command == "diagnose":
        if args.text_file:
            text = Path(args.text_file).read_text(encoding="utf-8")
        else:
            text = args.text or sys.stdin.read()
        if not text.strip():
            raise SystemExit("diagnose requires --text, --text-file, or stdin")
        diagnose_text(text, args.domain, args.limit)
    elif args.command == "rewrite":
        if args.text_file:
            text = Path(args.text_file).read_text(encoding="utf-8")
        else:
            text = args.text or sys.stdin.read()
        if not text.strip():
            raise SystemExit("rewrite requires --text, --text-file, or stdin")
        rewrite_text(text, args.domain, args.limit)


if __name__ == "__main__":
    main()
