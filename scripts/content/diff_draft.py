"""改稿回写(确定性·无 LLM):AI 稿 vs 你改稿 → 改写闸料(最高信号)+ diffs 表(改动量)。

你怎么改的,是系统学"像你/张芝士文风"的最强料。
- 单次改 = 捕获(每次改都抽出"AI这么写→你改成这样"的句对,存改写闸料桶)。
- 反复同类才提炼成文风范例(频次去重晋升,以后做;现在先全捕获)。
- edit_ratio(改动量)记进 diffs 表,随系统学会、你越改越少 = 升自主度的可衡量指标。

流程:
  1) 创作写出 data/topics/<id>/draft_v1.md,`save_draft <id> --author ai` 登记。
  2) 把 v1 另存为 draft_v2.md,在编辑器改,存盘。
  3) python scripts/content/diff_draft.py <id>
     → 登记 v2(author=human·approved)→ diff v1 vs v2 → 写改写闸料 + 记 diffs。
"""
import argparse
import difflib
import re
import sys
from datetime import date, datetime
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "scripts" / "collect"))
sys.stdout.reconfigure(encoding="utf-8", errors="replace")
from common import connect  # noqa: E402

DIFFS_DIR = ROOT / "data" / "diffs"                  # 改写闸料(中间燃料·归 humanizer 闸,非写手范例)
SENT = re.compile(r"[^。！？!?\n]*[。！？!?\n]|[^。！？!?\n]+")


def sentences(text: str) -> list[str]:
    return [s.strip() for s in SENT.findall(text) if s.strip()]


def char_edit_ratio(a: str, b: str) -> tuple[int, int, float]:
    """字符级改动量:总字数(去空白)、变动字数、比例。"""
    a2, b2 = re.sub(r"\s", "", a), re.sub(r"\s", "", b)
    matched = sum(blk.size for blk in difflib.SequenceMatcher(None, a2, b2).get_matching_blocks())
    total = max(len(a2), 1)
    changed = max(len(a2), len(b2)) - matched
    return changed, len(a2), round(changed / total, 3)


def main(topic_id: int) -> None:
    conn = connect()
    ai = conn.execute("SELECT * FROM drafts WHERE topic_id=? AND author='ai' "
                      "ORDER BY version DESC LIMIT 1", (topic_id,)).fetchone()
    if not ai:
        sys.exit(f"选题 {topic_id} 还没有 AI 稿(先创作 + save_draft --author ai)")
    out_dir = ROOT / "data" / "topics" / str(topic_id)
    ai_file = ROOT / ai["content_path"]
    files = sorted(out_dir.glob("draft_v*.md"))
    human_file = files[-1] if files else None
    if not human_file or human_file.resolve() == ai_file.resolve():
        sys.exit("没找到你改的版本。把 AI 稿另存为 draft_v2.md、改完再跑。")

    # 登记 human 稿(若未登记)+ 标 approved(=认可)
    rel_h = str(human_file.relative_to(ROOT))
    human = conn.execute("SELECT * FROM drafts WHERE topic_id=? AND content_path=?",
                         (topic_id, rel_h)).fetchone()
    with conn:
        if not human:
            ver = int(human_file.stem.split("v")[-1])
            cur = conn.execute(
                "INSERT INTO drafts(topic_id, account_id, version, stage, author, "
                "content_path, status) VALUES(?,?,?,?,?,?,?)",
                (topic_id, ai["account_id"], ver, "full", "human", rel_h, "approved"))
            human_id = cur.lastrowid
        else:
            human_id = human["id"]
            conn.execute("UPDATE drafts SET status='approved' WHERE id=?", (human_id,))

    ai_text = ai_file.read_text(encoding="utf-8")
    human_text = human_file.read_text(encoding="utf-8")
    a_sents, h_sents = sentences(ai_text), sentences(human_text)

    # 句级对齐 → 抽"AI句 → 你改句"对照对
    pairs = []
    for tag, i1, i2, j1, j2 in difflib.SequenceMatcher(None, a_sents, h_sents).get_opcodes():
        if tag == "equal":
            continue
        ai_part = " / ".join(a_sents[i1:i2]) or "(无,你新增)"
        hu_part = " / ".join(h_sents[j1:j2]) or "(无,你删了)"
        pairs.append((tag, ai_part, hu_part))

    changed, total, ratio = char_edit_ratio(ai_text, human_text)

    # 写改写闸料(data/diffs·带可检索 frontmatter:层/强度/时效/标签)
    DIFFS_DIR.mkdir(parents=True, exist_ok=True)
    note = DIFFS_DIR / f"对照_选题{topic_id}_{date.today():%Y%m%d}.md"
    body = [f"""---
type: 改写闸料
layer: humanizer闸(句级去AI味·非写手范例)
account: 张芝士
domain: 泛科普
strength: 0.9
strength_source: 改稿diff(最高信号)
topic_id: {topic_id}
edit_ratio: {ratio}
created: {date.today()}
tags: []
---

# 改写闸料 · 选题[{topic_id}](改动量 {ratio:.0%})

> AI 这么写 → 你改成这样。humanizer 闸校准料:照"你改的"学文风,把"AI 写的"当反面。**不进写手范例**。
"""]
    for k, (tag, ai_part, hu_part) in enumerate(pairs, 1):
        body.append(f"\n## {k}. [{tag}]\n- **AI**:{ai_part}\n- **你改**:{hu_part}")
    note.write_text("\n".join(body), encoding="utf-8")

    with conn:
        conn.execute(
            "INSERT INTO diffs(ai_draft_id, human_draft_id, diff_path, changed_chars, "
            "total_chars, edit_ratio, feedback_processed) VALUES(?,?,?,?,?,?,1)",
            (ai["id"], human_id, str(note.relative_to(ROOT)), changed, total, ratio))
    conn.close()
    print(f"改动量 {ratio:.0%}({changed}/{total} 字),抽出 {len(pairs)} 处对照。")
    print(f"改写闸料: {note.relative_to(ROOT)}")
    print("(单次=捕获;反复同类→晋升文风范例 待做)")


if __name__ == "__main__":
    p = argparse.ArgumentParser()
    p.add_argument("topic_id", type=int)
    main(p.parse_args().topic_id)
