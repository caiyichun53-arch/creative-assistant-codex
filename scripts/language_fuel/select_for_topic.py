"""Select a small language-fuel pack for a topic.

Deterministic retrieval only: no LLM. The output is meant to be embedded in
data/topics/<id>/brief.md so the creation workflow can use already-extracted
language-fuel insights.
"""
from __future__ import annotations

import argparse
import json
import math
import re
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "scripts" / "collect"))
sys.stdout.reconfigure(encoding="utf-8", errors="replace")

from common import connect  # noqa: E402

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

STAGE_WEIGHT = {
    "hook": 1.2,
    "style": 1.15,
    "material": 1.15,
    "topic": 1.0,
    "title": 0.9,
    "review": 0.75,
}

STOP_TOKENS = {
    "一个", "一种", "一些", "这个", "那个", "这种", "那种", "很多", "有人", "大家",
    "为什么", "为什", "什么", "怎么", "如何", "其实", "真正", "不是", "就是", "可能",
    "可以", "适合", "时候", "因为", "但是", "如果", "问题", "开头", "正文", "内容",
    "模式", "用法", "原句", "经验", "少量", "借鉴", "科普", "知识", "涨知", "领域",
    "财经", "涨知识", "燃起", "燃起来", "起来", "大国", "重器",
}


def _row_haystack(row) -> str:
    tags = " ".join(json.loads(row["tags"] or "[]"))
    examples = " ".join(json.loads(row["examples_json"] or "[]"))
    return f"{row['title'] or ''} {row['pattern']} {row['advice'] or ''} {tags} {examples}"


def _tokens(text: str) -> set[str]:
    text = re.sub(r"\s+", "", text or "")
    for phrase in sorted(STOP_TOKENS, key=len, reverse=True):
        if len(phrase) >= 2:
            text = text.replace(phrase, "")
    chunks = set(re.findall(r"[A-Za-z0-9_]{2,}", text.lower()))
    cjk = re.findall(r"[\u4e00-\u9fff]", text)
    chunks.update("".join(cjk[i:i + 2]) for i in range(max(0, len(cjk) - 1)))
    chunks.update("".join(cjk[i:i + 3]) for i in range(max(0, len(cjk) - 2)))
    return {x for x in chunks if x and x not in STOP_TOKENS}


def _topic_context(conn, topic_id: int) -> dict:
    row = conn.execute(
        """SELECT t.*, h.title AS hit_title, h.url AS hit_url, c.name AS competitor
           FROM topics t LEFT JOIN hits h ON h.id=t.source_hit_id
           LEFT JOIN competitor_accounts c ON c.id=h.competitor_id
           WHERE t.id=?""",
        (topic_id,),
    ).fetchone()
    if not row:
        raise SystemExit(f"topic not found: {topic_id}")
    return dict(row)


def _candidate_rows(conn, domain: str):
    return conn.execute(
        """SELECT * FROM language_fuel_insights
           WHERE status='active' AND domain=?
           ORDER BY strength DESC, created_at DESC""",
        (domain,),
    ).fetchall()


def _score(row, query_tokens: set[str]) -> tuple[float, int]:
    overlap = len(query_tokens & _tokens(_row_haystack(row)))
    strength = int(row["strength"] or 1)
    source_count = int(row["source_count"] or 0)
    stage = STAGE_WEIGHT.get(row["use_stage"], 1.0)
    score = (overlap * 3.0 + strength * 2.0 + math.log1p(source_count)) * stage
    return score, overlap


def select_for_topic(topic_id: int, limit: int = 3) -> str:
    conn = connect()
    try:
        topic = _topic_context(conn, topic_id)
        hit_title = str(topic.get("hit_title") or "").split("#", 1)[0]
        query = " ".join(
            str(x or "")
            for x in (topic.get("title"), topic.get("angle"), hit_title, topic.get("tags"))
        )
        qtokens = _tokens(query)
        rows = _candidate_rows(conn, topic["domain"])
        scored = sorted(((r, *_score(r, qtokens)) for r in rows), key=lambda x: x[1], reverse=True)

        selected = []
        per_category: dict[str, int] = {}
        per_stage: dict[str, int] = {}
        for row, score, overlap in scored:
            category = row["category"]
            stage = row["use_stage"]
            if overlap <= 0:
                continue
            if per_category.get(category, 0) >= 1:
                continue
            if per_stage.get(stage, 0) >= 1:
                continue
            selected.append(row)
            per_category[category] = per_category.get(category, 0) + 1
            per_stage[stage] = per_stage.get(stage, 0) + 1
            if len(selected) >= limit:
                break

        if not selected:
            return "（暂无可用语感经验。先运行爆款评论提炼脚本。）"

        blocks = [
            "> 只选 1 条最贴合当前选题的语感动作主用;其余只当语气参考,不要逐条塞进正文。"
        ]
        for row in selected:
            examples = json.loads(row["examples_json"] or "[]")[:3]
            example_lines = "\n".join(f"  - {x}" for x in examples)
            blocks.append(
                f"- [{CATEGORY_LABELS.get(row['category'], row['category'])} / {row['use_stage']} / 强度{row['strength']}]\n"
                f"  模式: {row['pattern']}\n"
                f"  用法: {row['advice'] or '按语境少量借鉴'}\n"
                f"  原句:\n{example_lines}"
            )
        return "\n".join(blocks)
    finally:
        conn.close()


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("topic_id", type=int)
    parser.add_argument("--limit", type=int, default=3)
    args = parser.parse_args()
    print(select_for_topic(args.topic_id, args.limit))


if __name__ == "__main__":
    main()
