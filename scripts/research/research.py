"""简版研究(LLM 生成节点,带网搜):选题+来源文案 → 材料+洞察包。

研究 ⟂ 创作:本脚本产出独立 artifact(data/topics/<id>/research.md),创作对话装载之,
研究噪音不污染创作上下文。深度旋钮:MVP 固定浅档;二期换深度研究循环。
用法: python scripts/research/research.py <topic_id>
"""
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "scripts" / "collect"))
sys.path.insert(0, str(ROOT / "scripts" / "llm"))
sys.stdout.reconfigure(encoding="utf-8", errors="replace")
from common import connect  # noqa: E402
from call import call_llm, load_prompt  # noqa: E402

# 提示词搬进 .claude/skills/研究/SKILL.md(唯一真相):二创=洗稿块、原创=原创块。
PROMPT = load_prompt("研究", "洗稿")            # 挂来源爆款,研究"竞品没讲的增量"
ORIGINAL_PROMPT = load_prompt("研究", "原创")   # 无来源,从零开题、不参照竞品


def main(topic_id: int) -> str:
    conn = connect()
    t = conn.execute(
        """SELECT t.*, h.transcript_path FROM topics t
           LEFT JOIN hits h ON h.id=t.source_hit_id WHERE t.id=?""",
        (topic_id,)).fetchone()
    if not t:
        sys.exit(f"选题 {topic_id} 不存在")
    # 只有二创(source_type='hit')走"找竞品没讲的"分支;原创/衍生(original/spinoff)走从零研究——
    # 衍生即便挂了 parent 爆款(出处),也是就新方向从零重搜,不参照那条爆款的文案。
    if t["source_type"] == "hit" and t["transcript_path"]:
        prompt = PROMPT.format(
            title=t["title"],
            transcript=(ROOT / t["transcript_path"]).read_text(encoding="utf-8"))
    else:
        prompt = ORIGINAL_PROMPT.format(title=t["title"])

    out = call_llm("research", prompt, allowed_tools=["WebSearch"], timeout=900)

    out_dir = ROOT / "data" / "topics" / str(topic_id)
    out_dir.mkdir(parents=True, exist_ok=True)
    path = out_dir / "research.md"
    path.write_text(out, encoding="utf-8")
    conn.close()
    print(f"研究包: {path.relative_to(ROOT)}")
    return str(path)


if __name__ == "__main__":
    main(int(sys.argv[1]))
