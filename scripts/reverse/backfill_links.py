"""一次性补链(幂等):给现存范例卡 + 路由表补 [[ ]] 双向链,对齐 reduce.py 新约定。
- 范例卡:加 '打法路由:[[{维度}打法路由表]]'(接通 范例 ↔ 方法论)
- 路由表:加 '## 关联爆款',列出「代表 hit」的 [[拆解笔记]](接通 方法论 ↔ 来源拆解)
范例卡的 [[来源拆解]] 链 reduce 早已写过,配合 vault/爆款拆解 junction 即生效,无需补。
跑法:python scripts/reverse/backfill_links.py
"""
import re
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "scripts" / "collect"))
sys.stdout.reconfigure(encoding="utf-8", errors="replace")
from common import connect  # noqa: E402

VAULT = ROOT / "vault"
DIMS = ("钩子", "结构", "选题")   # 文风维度已移除(不再从爆款金句产范例)


def cited_hits(table_text: str, valid_ids) -> list:
    """只读路由表每行最后一格(代表 hit 列),避开触发条件里的数字。"""
    seen, out = set(), []
    for line in table_text.splitlines():
        if "|" not in line:
            continue
        cells = [c.strip() for c in line.strip().strip("|").split("|")]
        if not cells:
            continue
        last = cells[-1]
        if "代表" in last or set(last) <= set("-: "):
            continue
        for m in re.finditer(r"\d+", last):
            hid = int(m.group())
            if hid in valid_ids and hid not in seen:
                seen.add(hid)
                out.append(hid)
    return out


def main():
    conn = connect()
    id2stem = {r["id"]: Path(r["dna_note_path"]).stem
               for r in conn.execute(
                   "SELECT id, dna_note_path FROM hits "
                   "WHERE dna_note_path IS NOT NULL AND dna_note_path!=''")}
    valid = set(id2stem)

    card_n = 0
    for card in (VAULT / "范例").rglob("*.md"):
        text = card.read_text(encoding="utf-8")
        if "打法路由" in text:
            continue
        m = re.search(r"^type:\s*(\S+)", text, re.M)
        if not m or m.group(1) not in DIMS:
            continue
        dim = m.group(1)
        card.write_text(text.rstrip() + f"\n- 打法路由:[[{dim}打法路由表]]\n", encoding="utf-8")
        card_n += 1

    meth_n = 0
    for mp in (VAULT / "方法论").glob("*打法路由表.md"):
        text = mp.read_text(encoding="utf-8")
        if "关联爆款" in text:
            continue
        parts = text.split("---", 2)
        body = parts[-1] if len(parts) == 3 else text
        hits = cited_hits(body, valid)
        if not hits:
            continue
        links = "\n".join(f"- [[{id2stem[h]}]](hit {h})" for h in hits)
        mp.write_text(text.rstrip() + "\n\n## 关联爆款(点进去看完整拆解)\n" + links + "\n",
                      encoding="utf-8")
        meth_n += 1

    print(f"补链完成:范例卡 {card_n} 张 + 路由表 {meth_n} 张")


if __name__ == "__main__":
    main()
