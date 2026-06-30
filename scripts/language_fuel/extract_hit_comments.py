"""Extract classified language-fuel insights from hit comments.

This is the LLM step after deterministic collection:
hit_comments -> classified insights -> SQLite + Obsidian raw insight notes.

It does not crawl any platform. It only reads comments that already exist in
SQLite from reverse preparation.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import re
import sys
from datetime import date
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "scripts" / "collect"))
sys.path.insert(0, str(ROOT / "scripts" / "llm"))
sys.stdout.reconfigure(encoding="utf-8", errors="replace")

from call import SessionLimitError, call_llm  # noqa: E402
from common import connect, safe_title  # noqa: E402

OUT_DIR = ROOT / "data" / "language_fuel" / "hit_comments"
VAULT_DIR = ROOT / "vault" / "语感燃料" / "提炼" / "爆款评论"

CATEGORY_LABELS = {
    "meme": "玩梗/网络化表达",
    "proverb": "自创歇后语/类比句",
    "rhyme": "押韵/顺口句",
    "punchline": "金句/神评论",
    "oral": "网络口语/真人语气",
    "hot_word": "热门延伸词/新词",
    "emotion": "情绪立场/共鸣表达",
    "material": "选题资料/用户洞察",
}
USE_STAGES = {"topic", "hook", "style", "material", "title", "review"}

PROMPT = """你是中文短视频写作系统里的“语感燃料提炼员”。
下面是同一个已验证爆款视频下的真实网友评论。请只从评论里提炼可复用的写作经验和内容洞察。

边界:
- 不要摘要视频, 不要写泛泛而谈的方法论。
- 保留真实网友原句作为例子, 但不要编造评论。
- 输出是给后续创作检索用的经验卡, 不是禁令。
- 分类要克制, 宁缺毋滥。没有价值的类别不要输出。

可用 category:
- meme: 玩梗/网络化表达
- proverb: 自创歇后语/类比句
- rhyme: 押韵/顺口句
- punchline: 金句/神评论
- oral: 网络口语/真人语气
- hot_word: 热门延伸词/新词
- emotion: 情绪立场/共鸣表达
- material: 选题资料/用户洞察

use_stage 只能选:
- topic: 选题判断
- hook: 开头钩子
- style: 文风语气
- material: 内容资料
- title: 标题
- review: 审稿润色

视频信息:
标题: {title}
领域: {domain}
对标账号: {competitor}
爆款强度: 超额 {excess_ratio}x

评论清单:
{comments}

请严格输出 JSON, 不要包 markdown 代码块:
{{
  "insights": [
    {{
      "category": "meme",
      "use_stage": "style",
      "pattern": "一句话写清这个语感/打法, 必须具体",
      "examples": ["评论原句1", "评论原句2"],
      "advice": "创作时怎么用, 一两句即可",
      "tags": ["短标签1", "短标签2"],
      "strength": 1
    }}
  ]
}}
"""


def _comment_block(rows) -> str:
    lines = []
    for i, row in enumerate(rows, 1):
        like = row["like_count"] or 0
        reply = row["sub_comment_count"] or 0
        text = re.sub(r"\s+", " ", row["content"] or "").strip()
        lines.append(f"{i}. [{like}赞/{reply}回复] {text}")
    return "\n".join(lines)


def _hash(text: str) -> str:
    return hashlib.sha1(text.encode("utf-8")).hexdigest()[:16]


def _strip_code_fence(text: str) -> str:
    text = text.strip()
    if text.startswith("```"):
        text = re.sub(r"^```(?:json)?", "", text).strip()
        text = re.sub(r"```$", "", text).strip()
    return text


def _parse_output(text: str) -> list[dict]:
    data = json.loads(_strip_code_fence(text))
    insights = data.get("insights", data if isinstance(data, list) else [])
    out = []
    for item in insights:
        if not isinstance(item, dict):
            continue
        category = str(item.get("category") or "").strip()
        use_stage = str(item.get("use_stage") or "style").strip()
        pattern = str(item.get("pattern") or "").strip()
        examples = item.get("examples") or []
        if category not in CATEGORY_LABELS or use_stage not in USE_STAGES:
            continue
        if not pattern or not isinstance(examples, list) or not examples:
            continue
        clean_examples = [str(x).strip() for x in examples if str(x).strip()]
        if not clean_examples:
            continue
        try:
            strength = max(1, min(5, int(item.get("strength") or 3)))
        except (TypeError, ValueError):
            strength = 3
        tags = item.get("tags") or []
        if not isinstance(tags, list):
            tags = [str(tags)]
        out.append({
            "category": category,
            "use_stage": use_stage,
            "pattern": pattern,
            "examples": clean_examples[:5],
            "advice": str(item.get("advice") or "").strip(),
            "tags": [str(t).strip() for t in tags if str(t).strip()][:8],
            "strength": strength,
        })
    return out


def _source_strength(excess_ratio, insight_strength: int) -> int:
    try:
        excess = float(excess_ratio or 0)
    except (TypeError, ValueError):
        excess = 0
    source_bonus = 2 if excess >= 8 else 1 if excess >= 4 else 0
    return max(1, min(5, insight_strength + source_bonus))


def _write_files(hit, insights: list[dict], no_obsidian: bool) -> str | None:
    domain = hit["domain"] or "未分类"
    title = safe_title(hit["title"], hit["id"])
    folder = OUT_DIR / domain / f"{hit['id']}_{title}"
    folder.mkdir(parents=True, exist_ok=True)
    payload = {
        "source_type": "hit_comments",
        "source_id": hit["id"],
        "platform": "douyin",
        "domain": domain,
        "title": hit["title"],
        "insights": insights,
    }
    (folder / "insights.json").write_text(
        json.dumps(payload, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )
    if no_obsidian:
        return None

    vault_folder = VAULT_DIR / domain
    vault_folder.mkdir(parents=True, exist_ok=True)
    note = vault_folder / f"{title}_{hit['id']}.md"
    blocks = []
    for item in insights:
        examples = "\n".join(f"- {x}" for x in item["examples"])
        tags = ", ".join(item["tags"])
        blocks.append(
            f"## {CATEGORY_LABELS[item['category']]} / {item['use_stage']}\n"
            f"- 模式: {item['pattern']}\n"
            f"- 用法: {item['advice']}\n"
            f"- 强度: {item['strength']}\n"
            f"- 标签: {tags}\n\n"
            f"原句例子:\n{examples}\n"
        )
    note.write_text(
        f"""---
type: language_fuel_insight_batch
source_type: hit_comments
platform: douyin
domain: {domain}
source_id: {hit['id']}
source_title: "{str(hit['title'] or '').replace('"', '')}"
status: active
created: {date.today()}
usage: 创作材料包按相关度少量检索; 不整篇进 prompt; 不等于正式范例
---

# 爆款评论语感提炼: {hit['title']}

> 来源: {hit['competitor']} | 超额 {hit['excess_ratio']}x | {hit['url'] or ''}

{chr(10).join(blocks)}
""",
        encoding="utf-8",
    )
    return str(note.relative_to(ROOT))


def _store(conn, hit, insights: list[dict], note_path: str | None) -> int:
    written = 0
    with conn:
        for item in insights:
            examples_json = json.dumps(item["examples"], ensure_ascii=False)
            tags_json = json.dumps(item["tags"], ensure_ascii=False)
            content_hash = _hash(item["category"] + item["pattern"] + examples_json)
            strength = _source_strength(hit["excess_ratio"], item["strength"])
            cur = conn.execute(
                """INSERT OR IGNORE INTO language_fuel_insights
                   (source_type, source_id, platform, domain, category, use_stage,
                    title, pattern, examples_json, advice, tags, strength,
                    source_count, obsidian_note, content_hash)
                   VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                (
                    "hit_comments",
                    str(hit["id"]),
                    "douyin",
                    hit["domain"],
                    item["category"],
                    item["use_stage"],
                    hit["title"],
                    item["pattern"],
                    examples_json,
                    item["advice"],
                    tags_json,
                    strength,
                    len(item["examples"]),
                    note_path,
                    content_hash,
                ),
            )
            if cur.rowcount > 0:
                written += 1
    return written


def _pending_hits(conn, hit_id: int | None, limit_hits: int):
    base = (
        "SELECT h.*, c.domain AS domain, c.name AS competitor "
        "FROM hits h JOIN competitor_accounts c ON c.id=h.competitor_id "
        "WHERE EXISTS (SELECT 1 FROM hit_comments hc WHERE hc.hit_id=h.id)"
    )
    params: list[object] = []
    if hit_id:
        base += " AND h.id=?"
        params.append(hit_id)
    else:
        base += (
            " AND NOT EXISTS (SELECT 1 FROM language_fuel_insights l "
            "WHERE l.source_type='hit_comments' AND l.source_id=CAST(h.id AS TEXT))"
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


def run(hit_id: int | None, limit_hits: int, comments_per_hit: int,
        dry_run: bool, no_obsidian: bool, force: bool) -> None:
    conn = connect()
    try:
        if force and hit_id:
            with conn:
                conn.execute(
                    "DELETE FROM language_fuel_insights "
                    "WHERE source_type='hit_comments' AND source_id=?",
                    (str(hit_id),),
                )
        hits = _pending_hits(conn, hit_id, limit_hits)
        if not hits:
            print("没有待提炼的爆款评论。")
            return
        for hit in hits:
            comments = _comments_for_hit(conn, hit["id"], comments_per_hit)
            if not comments:
                continue
            prompt = PROMPT.format(
                title=hit["title"] or "",
                domain=hit["domain"] or "",
                competitor=hit["competitor"] or "",
                excess_ratio=hit["excess_ratio"] or "",
                comments=_comment_block(comments),
            )
            try:
                raw = call_llm("language_fuel_extract", prompt, timeout=600)
            except SessionLimitError:
                raise
            insights = _parse_output(raw)
            if dry_run:
                print(f"\n[hit {hit['id']}] {hit['title']} -> {len(insights)} 条")
                print(json.dumps(insights, ensure_ascii=False, indent=2))
                continue
            note_path = _write_files(hit, insights, no_obsidian=no_obsidian)
            written = _store(conn, hit, insights, note_path)
            print(f"hit {hit['id']}: 提炼 {len(insights)} 条, 入库 {written} 条")
    finally:
        conn.close()


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--hit", type=int, help="只提炼指定爆款")
    parser.add_argument("--limit-hits", type=int, default=5)
    parser.add_argument("--comments-per-hit", type=int, default=80)
    parser.add_argument("--dry-run", action="store_true")
    parser.add_argument("--no-obsidian", action="store_true")
    parser.add_argument("--force", action="store_true", help="配合 --hit 重新提炼并覆盖该 hit 的旧入库结果")
    args = parser.parse_args()
    run(args.hit, args.limit_hits, args.comments_per_hit, args.dry_run, args.no_obsidian, args.force)


if __name__ == "__main__":
    main()
