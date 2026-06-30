"""开篇收集器(确定性·无 LLM):把已转写爆款的"前3秒开篇原句"捞进钩子范例库的待归类池。

经验迭代的入口:钩子范例库 vault/范例/钩子/_开篇范例.md 是人工按"五招"归好的正式库;
本脚本把新爆款的真开篇原句确定性地捞出来,落进 `_开篇·新收待归类.md`,
等人工(或后续轻量归类)把好的挪进正式库的对应招下。**不调 LLM、不做招分析**——
只搬真原句(钩子要的就是真人开篇的手感),招怎么归是另一回事。

幂等:每次全量重建待归类池;已在正式库里出现过(按〔hit N〕)的开篇自动跳过,不重复收。

用法:
  python scripts/reverse/collect_openings.py            # 重建待归类池
  python scripts/reverse/collect_openings.py --domain 泛科普
"""
import argparse
import re
import sys
from datetime import date
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "scripts" / "collect"))
sys.stdout.reconfigure(encoding="utf-8", errors="replace")
from common import connect  # noqa: E402

HOOK_DIR = ROOT / "vault" / "范例" / "钩子"
CURATED = HOOK_DIR / "_开篇范例.md"          # 正式库(人工按五招归好的)
POOL = HOOK_DIR / "_开篇·新收待归类.md"        # 待归类池(本脚本产)

OPENING_TARGET = 50      # 开篇窗口目标字数(口播"前3秒"约此量)
OPENING_MAX_SENT = 3     # 最多取几句
_SENT_END = re.compile(r"[。！？!?…]+")


def _opening_window(transcript: str) -> str:
    """口播文案 → 前3秒开篇窗口(确定性):累计句子到约 OPENING_TARGET 字或 OPENING_MAX_SENT 句。"""
    text = transcript.strip().replace("\n", "")
    parts, idx = [], 0
    for m in _SENT_END.finditer(text):
        parts.append(text[idx:m.end()])
        idx = m.end()
    if idx < len(text):
        parts.append(text[idx:])
    out = ""
    for i, s in enumerate(parts):
        out += s
        if (len(out) >= OPENING_TARGET or (i + 1) >= OPENING_MAX_SENT):
            break
    return out.strip()


def _already_curated() -> set[int]:
    """正式库里已收录(按〔hit N〕)的 hit 编号——这些不再进待归类池。"""
    if not CURATED.exists():
        return set()
    return {int(m.group(1)) for m in re.finditer(r"〔hit\s*(\d+)", CURATED.read_text(encoding="utf-8"))}


def collect(domain: str | None) -> None:
    conn = connect()
    q = ("SELECT h.id, h.title, h.excess_ratio, h.transcript_path, c.domain "
         "FROM hits h JOIN competitor_accounts c ON c.id=h.competitor_id "
         "WHERE h.transcript_path IS NOT NULL AND h.transcript_path!='' "
         + ("AND c.domain=? " if domain else "")
         + "ORDER BY c.domain, h.excess_ratio DESC")
    rows = conn.execute(q, (domain,) if domain else ()).fetchall()
    conn.close()

    done = _already_curated()
    by_domain: dict[str, list] = {}
    for h in rows:
        if h["id"] in done:
            continue
        tp = ROOT / h["transcript_path"]
        if not tp.exists():
            continue
        opening = _opening_window(tp.read_text(encoding="utf-8"))
        if opening:
            by_domain.setdefault(h["domain"] or "(未分领域)", []).append((h, opening))

    n = sum(len(v) for v in by_domain.values())
    out = [f"""---
type: 范例库·开篇·待归类
bucket: 钩子
status: 待人工归类(确定性收集,无招分析)
created: {date.today()}
usage: collect_openings.py 确定性捞的新爆款开篇原句,按领域列。人工把好的挪进 _开篇范例.md 对应的五招下,挪走后下次重建本池它会消失(正式库已收=不再进池)。
---

# 开篇·待归类池（{n} 条 · 等归进五招）

> 这些是确定性捞出的真开篇原句,**还没归招**。挑出值得当范例的,搬进 `_开篇范例.md` 对应策略下(配一句"为什么留得住"),并保留〔hit N〕。搬走的下次重建会自动从本池消失。
"""]
    for dom, items in by_domain.items():
        out.append(f"\n## {dom}（{len(items)} 条）\n")
        for h, opening in items:
            out.append(f"- \"{opening}\" 〔hit {h['id']} · 超额{h['excess_ratio']}x · {h['title'][:24]}〕")

    HOOK_DIR.mkdir(parents=True, exist_ok=True)
    POOL.write_text("\n".join(out) + "\n", encoding="utf-8")
    print(f"✓ 待归类池: {POOL.relative_to(ROOT)}（{n} 条新开篇,已排除正式库收录的 {len(done)} 条）")


def main() -> None:
    p = argparse.ArgumentParser()
    p.add_argument("--domain", help="限定领域(默认全领域)")
    collect(p.parse_args().domain)


if __name__ == "__main__":
    main()
