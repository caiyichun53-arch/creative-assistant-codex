"""禁词校验器(确定性·无 LLM):扫描成稿,按 config/banned_words.yaml 出报告。

定位:阶段5 的 L1 闸,跑在篇级审稿与 humanizer 润色之间。禁令只在这里(写完后扫),
绝不进生成 prompt(章程:禁令进校验器)。命中 red / red_patterns → 退出码 1(可当闸拦)。

用法:
  python scripts/content/check_banned.py <topic_id>        # 扫 data/topics/<id>/draft_v1.md(最新版)
  python scripts/content/check_banned.py --file <path>     # 扫任意文件
  python scripts/content/check_banned.py --version 2 <id>  # 指定版本

等级:red=出现1次即拦;warn=800字内≥2次告警;hint=提示不拦(书面词/翻译腔)。
四字成语那条需成语词典,暂不自动判(免误杀),留 LLM 审稿清单兜。
"""
import argparse
import re
import sys
from pathlib import Path

import yaml

ROOT = Path(__file__).resolve().parents[2]
sys.stdout.reconfigure(encoding="utf-8", errors="replace")
CONFIG = ROOT / "config" / "banned_words.yaml"


def line_of(text: str, pos: int) -> int:
    """字符位置 → 行号(1-based)。"""
    return text.count("\n", 0, pos) + 1


def find_all(text: str, term: str):
    """返回 term 在 text 里所有出现的起始位置。"""
    out, start = [], 0
    while True:
        i = text.find(term, start)
        if i < 0:
            break
        out.append(i)
        start = i + 1
    return out


def check(text: str, cfg: dict):
    reds, warns, hints = [], [], []

    # red:逐类逐词,出现即拦
    for cat, terms in (cfg.get("red") or {}).items():
        for term in terms:
            for pos in find_all(text, term):
                reds.append((cat, term, line_of(text, pos)))

    # warn:800 字内同词 ≥2 次
    for cat, terms in (cfg.get("warn") or {}).items():
        for term in terms:
            pos = find_all(text, term)
            for a, b in zip(pos, pos[1:]):
                if b - a < 800:
                    warns.append((cat, term, line_of(text, a), line_of(text, b), len(pos)))
                    break

    # red_patterns:正则命中即拦
    for name, pat in (cfg.get("red_patterns") or {}).items():
        for m in re.finditer(pat, text):
            reds.append((f"模式·{name}", m.group(0).replace("\n", "⏎"), line_of(text, m.start())))

    # hint_patterns:翻译腔等,提示不拦
    for name, pat in (cfg.get("hint_patterns") or {}).items():
        for m in re.finditer(pat, text):
            hints.append((f"翻译腔·{name}", m.group(0), line_of(text, m.start())))

    # 书面词 → 口语替代:提示 + 给建议
    for formal, spoken in (cfg.get("replacements") or {}).items():
        for pos in find_all(text, formal):
            hints.append((f"书面词→{spoken}", formal, line_of(text, pos)))

    # 标点(口播):分号一律告警;每段感叹号 >2 告警
    for ln_no, ln in enumerate(text.splitlines(), 1):
        if "；" in ln or ";" in ln:
            warns.append(("标点·分号", "；(口语用句号断开)", ln_no, ln_no, ln.count("；") + ln.count(";")))
        bangs = ln.count("！") + ln.count("!")
        if bangs > 2:
            warns.append(("标点·感叹号", f"本段 {bangs} 个！(建议≤2)", ln_no, ln_no, bangs))

    return reds, warns, hints


def main() -> None:
    p = argparse.ArgumentParser()
    p.add_argument("topic_id", type=int, nargs="?", help="选题 id(扫其 draft)")
    p.add_argument("--file", help="直接扫某文件(优先于 topic_id)")
    p.add_argument("--version", type=int, help="指定 draft 版本号(缺省=最新)")
    a = p.parse_args()

    if a.file:
        path = Path(a.file)
    elif a.topic_id is not None:
        d = ROOT / "data" / "topics" / str(a.topic_id)
        if a.version:
            path = d / f"draft_v{a.version}.md"
        else:
            vs = sorted(d.glob("draft_v*.md"))
            if not vs:
                sys.exit(f"没找到稿件:{d}/draft_v*.md")
            path = vs[-1]
    else:
        sys.exit("给个 topic_id 或 --file")

    if not path.exists():
        sys.exit(f"文件不存在:{path}")

    text = path.read_text(encoding="utf-8")
    cfg = yaml.safe_load(CONFIG.read_text(encoding="utf-8"))
    reds, warns, hints = check(text, cfg)

    print(f"# 禁词校验 · {path.relative_to(ROOT) if path.is_relative_to(ROOT) else path}")
    print(f"  字数 {len(text)} | RED {len(reds)} · WARN {len(warns)} · HINT {len(hints)}\n")

    if reds:
        print("【RED · 命中即拦,必须定点改】")
        for cat, term, ln in reds:
            print(f"  L{ln}  [{cat}] 「{term}」")
        print()
    if warns:
        print("【WARN · 告警,建议改】")
        for w in warns:
            cat, term, l1 = w[0], w[1], w[2]
            extra = f"(L{w[2]}↔L{w[3]}, 共{w[4]}次)" if len(w) >= 5 and w[3] != w[2] else ""
            print(f"  L{l1}  [{cat}] 「{term}」{extra}")
        print()
    if hints:
        print("【HINT · 提示,自行判断】")
        for cat, term, ln in hints:
            print(f"  L{ln}  [{cat}] 「{term}」")
        print()

    if not (reds or warns or hints):
        print("✓ 干净,无命中。")

    print("注:四字成语(同段≥2)需成语词典,本校验器不自动判,留审稿清单兜。")
    sys.exit(1 if reds else 0)


if __name__ == "__main__":
    main()
