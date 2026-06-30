"""范例卡聚合(确定性·无 LLM):把 vault/范例/{桶}/ 的散卡聚成一份 `_{桶}范例合集.md`。

为什么要它(2026-06-22,治钩子塌的真根):
  写手是子 agent,工具只有 Read/Write/Edit/Skill——**没有列目录/搜索的能力**,无法发现
  `vault/范例/钩子/` 里有哪些卡文件。可钩子 skill 第3步(取范例拆手感)却让写手"自己去桶里按
  play 取卡"→ 它一张都拿不到 → skill 最关键一步空转 → 退化成套公式(出套话、为凑句式拧断逻辑)。
  修法 = 范例卡的【递送】归流水线:本脚本把整桶卡聚成**一份固定路径**的合集(按 play 分组),
  skill 让写手 Read 这一份即可。写手不再做任何"发现文件"的活。

确定性:纯读卡 + 重排,不调 LLM。卡本体一字不动地搬进合集(写手要的是真钩子原句的手感)。
幂等:每次全量重建合集(卡是唯一真源,合集是派生物)。reduce.py 产新卡后重跑本脚本即可。
合集文件名带前缀 `_`,聚合时自动跳过(不会把合集自己当成一张卡)。

用法:
  python scripts/reverse/build_card_digest.py            # 重建所有桶的合集
  python scripts/reverse/build_card_digest.py --bucket 钩子   # 只重建钩子桶
"""
import argparse
import re
import sys
from datetime import date
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
sys.stdout.reconfigure(encoding="utf-8", errors="replace")
EXAMPLES = ROOT / "vault" / "范例"

# 合集里要保留的卡内容段(按 ## 前缀匹配);顺序即呈现顺序。点题取 H1。
KEEP_SECTIONS = ["范例本体", "AI 会怎么写砸", "为什么有效", "怎么照做"]


def _frontmatter(text: str) -> dict:
    m = re.match(r"^---\n(.*?)\n---\n", text, re.S)
    fm = {}
    if m:
        for line in m.group(1).splitlines():
            if ":" in line and not line.startswith(" "):
                k, _, v = line.partition(":")
                fm[k.strip()] = v.strip().strip('"').strip("'")
    return fm


def _body(text: str) -> str:
    m = re.match(r"^---\n.*?\n---\n(.*)$", text, re.S)
    return m.group(1) if m else text


def _h1(body: str) -> str:
    for line in body.splitlines():
        if line.startswith("# "):
            return line[2:].strip()
    return ""


def _sections(body: str) -> dict:
    secs, cur, buf = {}, None, []
    for line in body.splitlines():
        if line.startswith("## "):
            if cur is not None:
                secs[cur] = "\n".join(buf).strip()
            cur, buf = line[3:].strip(), []
        elif cur is not None:
            buf.append(line)
    if cur is not None:
        secs[cur] = "\n".join(buf).strip()
    return secs


def _strength(fm: dict) -> int:
    try:
        return int(fm.get("strength", 0))
    except ValueError:
        return 0


def build_bucket(bucket: str) -> Path | None:
    folder = EXAMPLES / bucket
    if not folder.exists():
        print(f"[{bucket}] 桶不存在,跳过: {folder}")
        return None
    cards = []
    for f in sorted(folder.glob("*.md")):
        if f.name.startswith("_"):           # 跳过合集自己 / 索引
            continue
        text = f.read_text(encoding="utf-8")
        fm = _frontmatter(text)
        body = _body(text)
        cards.append({
            "play": fm.get("play", "(未标 play)"),
            "strength": _strength(fm),
            "sid": fm.get("source_id", "?"),
            "点题": _h1(body),
            "secs": _sections(body),
            "file": f.name,
        })
    if not cards:
        print(f"[{bucket}] 桶里没有卡,跳过。")
        return None

    # 按 play 分组,组内按强度降序
    by_play: dict[str, list] = {}
    for c in cards:
        by_play.setdefault(c["play"], []).append(c)
    for lst in by_play.values():
        lst.sort(key=lambda c: c["strength"], reverse=True)

    out = [f"""---
type: 范例合集
bucket: {bucket}
source: 聚合自 vault/范例/{bucket}/ 各卡(build_card_digest.py·确定性重建)
card_n: {len(cards)}
created: {date.today()}
usage: 写手在「{bucket}」这一拍 Read 本文件即可——验证过的范例全在此、已按 play 分组;写手不用去目录找卡
---

# {bucket}范例合集(按 play 分组 · 写手读这一份)

> 这是 `vault/范例/{bucket}/` 全部 {len(cards)} 张卡的聚合,**已按打法(play)分好组**。
> **这些卡来自爆款,只给你"招"(打法/结构),不给你"话"(措辞)。** 爆款可能是 AI 写的,照搬它的词 = AI 味回流。
> 写手:翻到你选中那几招那组,挑最强的 1-2 张——先看「AI 会怎么写砸」避坑,再看「范例本体」**看清这招怎么摆出来的**(不是学它的措辞),再看机制和怎么照做。
> **招从这里来,话从 真人写作基石 + 评论真人味基石 来。**
"""]
    for play, lst in sorted(by_play.items(), key=lambda kv: -max(c["strength"] for c in kv[1])):
        out.append(f"\n---\n\n## 打法:{play}（{len(lst)} 张）\n")
        for c in lst:
            out.append(f"### [强度{c['strength']} · hit {c['sid']}] {c['点题']}")
            for name in KEEP_SECTIONS:
                # 段标题可能带括号(如 "AI 会怎么写砸(反例)"),按前缀匹配
                val = next((v for k, v in c["secs"].items() if k.startswith(name)), "")
                if val:
                    out.append(f"**{name}**:{val}")
            out.append("")
    path = folder / f"_{bucket}范例合集.md"
    path.write_text("\n".join(out), encoding="utf-8")
    print(f"[{bucket}] ✓ 合集 {path.relative_to(ROOT)}(聚了 {len(cards)} 张卡 · {len(by_play)} 类打法)")
    return path


# 钩子已退出"卡→合集"机制:钩子方法论+例子改用手搓的 vault/方法论/钩子打法.md(留存优先·老框架)。
# 不再聚合钩子合集(2026-06-23,A/B 证伪旧 play-分组例子库)。
SKIP_BUCKETS = {"钩子"}


def main() -> None:
    p = argparse.ArgumentParser()
    p.add_argument("--bucket", help="只重建某个桶(默认所有桶)")
    a = p.parse_args()
    if not EXAMPLES.exists():
        sys.exit(f"范例库不存在: {EXAMPLES}")
    buckets = [a.bucket] if a.bucket else [d.name for d in sorted(EXAMPLES.iterdir()) if d.is_dir()]
    for b in buckets:
        if b in SKIP_BUCKETS:
            print(f"[{b}] 已退出合集机制(方法论手搓在 vault/方法论/钩子打法.md),跳过。")
            continue
        build_bucket(b)


if __name__ == "__main__":
    main()
