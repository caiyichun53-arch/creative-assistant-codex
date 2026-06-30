"""评论真人味基石提炼(reduce·LLM 生成节点):爆款评论语感燃料 → 一份 always-on 语感基石。

定位(对齐 comment-humanvoice-base-plan / kouban-grounding-gap):
  写手写口播,垫的真人写作基石是公众号【书面】文章;缺一块"真人开口随口说话"的语感。
  评论区是观众真说的话 = 最干净的口语语感源。但现成的语感燃料是【一视频一文件】的原始提炼,
  且混两层:① 大家"怎么说话"(口语/语气/玩梗/类比)= 真语感料;② 这条选题的痛点/争议 = 选题材料。
  本脚本把所有评论语感燃料里**只属第①层(打了 `/ style` 标的段)**横抽出来,map-reduce 熬成
  **一个** vault/评论真人味基石.md —— 和 真人写作基石 并列、全量注入写手。第②层留在原燃料里按选题检索,不进基石。

确定性 vs 生成(守章程):
  - 代码(确定性):走文件、按 `## 分类 / 层标` 的层标过滤只留 style、拼清单、加 [[来源]] 链。
  - LLM(生成·human_flavor=sonnet):只把清单归纳成"真人怎么说话"的共性范例 + 真原句。prompt 写死,LLM 不握流程。

少而厚 / 范例非规则:产正向语感范例(每条带真实评论原句为证),不产禁令清单。
预算/续跑:走 call.py 熔断;量大超预算时分组多级归并(沿用 extract.py 配方),撞限干净中止。
幂等:成品已存在即跳过(--force 重跑);--dry-run 只打印清单+产出不落盘。

用法:
  python scripts/humanize/comment_voice.py --status            # 看有多少源、是否已产
  python scripts/humanize/comment_voice.py --dry-run           # 抽清单 + 跑归纳,打印不落盘(先看)
  python scripts/humanize/comment_voice.py                     # 落盘到 vault/评论真人味基石.md
  python scripts/humanize/comment_voice.py --domain 泛科普      # 限定领域(默认取燃料里第一个领域)
  python scripts/humanize/comment_voice.py --force             # 成品已存在也重跑
"""
import argparse
import re
import sys
from datetime import date, datetime
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "scripts" / "llm"))
sys.stdout.reconfigure(encoding="utf-8", errors="replace")
from call import call_llm, load_prompt, SessionLimitError  # noqa: E402

FUEL_DIR = ROOT / "vault" / "语感燃料" / "提炼" / "爆款评论"
OUT_FILE = ROOT / "vault" / "评论真人味基石.md"
LOG_FILE = ROOT / "logs" / "comment_voice.log"

VOICE_LAYER = "style"      # 只取这一层:观众"怎么说话"(语气/口语/玩梗/类比),非选题痛点
REDUCE_CHARS = 80000       # 单次归并清单字符预算(超则分组多级归并,沿用 humanize)

PROMPT = load_prompt("评论真人味", "map")          # 提示词搬进 .claude/skills/评论真人味/SKILL.md
REDUCE_GROUP_PROMPT = load_prompt("评论真人味", "reduce")


def _log(msg: str) -> None:
    line = f"{datetime.now():%m-%d %H:%M:%S} {msg}"
    print(line)
    try:
        LOG_FILE.parent.mkdir(parents=True, exist_ok=True)
        with LOG_FILE.open("a", encoding="utf-8") as fh:
            fh.write(line + "\n")
    except Exception:
        pass


def _frontmatter(text: str) -> dict:
    """抽 --- ... --- 里的简单 key: value(只取标量,够用)。"""
    m = re.match(r"^---\n(.*?)\n---\n", text, re.S)
    fm = {}
    if m:
        for line in m.group(1).splitlines():
            if ":" in line and not line.startswith(" "):
                k, _, v = line.partition(":")
                fm[k.strip()] = v.strip().strip('"').strip("'")
    return fm


def _style_sections(text: str) -> list[tuple[str, str]]:
    """切 '## 分类 / 层标' 段,只留层标==style 的;返回 [(分类名, 段正文), ...]。"""
    out, cur_head, buf = [], None, []

    def flush():
        if cur_head is not None:
            cat, _, layer = cur_head.rpartition("/")
            if layer.strip() == VOICE_LAYER:
                out.append((cat.strip(), "\n".join(buf).strip()))

    for line in text.splitlines():
        if line.startswith("## "):
            flush()
            cur_head, buf = line[3:].strip(), []
        elif cur_head is not None:
            buf.append(line)
    flush()
    return out


def _domains() -> list[str]:
    if not FUEL_DIR.exists():
        return []
    return sorted(p.name for p in FUEL_DIR.iterdir() if p.is_dir())


def _gather(domain: str):
    """走某领域所有评论语感燃料,抽 style 层 → (digest, [源文件 stem,...], n)。不耗 LLM。"""
    ddir = FUEL_DIR / domain
    files = sorted(ddir.glob("*.md")) if ddir.exists() else []
    blocks, sources = [], []
    for f in files:
        text = f.read_text(encoding="utf-8")
        secs = _style_sections(text)
        if not secs:
            continue
        fm = _frontmatter(text)
        title = fm.get("source_title", f.stem)
        sid = fm.get("source_id", "?")
        sources.append(f.stem)
        body = "\n".join(f"### {cat}\n{txt}" for cat, txt in secs)
        blocks.append(f"[来源 hit {sid} | {title}]\n{body}")
    return blocks, sources, len(blocks)


def _reduce(domain: str, blocks: list[str]) -> str:
    """blocks → 归纳文本。一趟装不下就分组多级归并(沿用 humanize 配方)。"""
    n = len(blocks)
    joined = "\n\n".join(blocks)
    if len(joined) <= REDUCE_CHARS:
        return call_llm("human_flavor", PROMPT.format(domain=domain, n=n, digest=joined),
                        timeout=1800)
    # 超预算:先分组各归一次,再把组笔记归并(可多级)
    _log(f"清单 {len(joined)} 字超预算 {REDUCE_CHARS},分组多级归并。")
    notes = []
    group, gchars = [], 0
    for b in blocks:
        if group and gchars + len(b) > REDUCE_CHARS:
            notes.append(call_llm("human_flavor",
                                  PROMPT.format(domain=domain, n=len(group),
                                                digest="\n\n".join(group)), timeout=1800))
            group, gchars = [], 0
        group.append(b)
        gchars += len(b)
    if group:
        notes.append(call_llm("human_flavor",
                              PROMPT.format(domain=domain, n=len(group),
                                            digest="\n\n".join(group)), timeout=1800))
    while len(notes) > 1:
        merged, grp, gchars = [], [], 0
        for nt in notes:
            if grp and gchars + len(nt) > REDUCE_CHARS:
                merged.append(call_llm("human_flavor",
                                       REDUCE_GROUP_PROMPT.format(notes="\n\n=====\n\n".join(grp)),
                                       timeout=1800))
                grp, gchars = [], 0
            grp.append(nt)
            gchars += len(nt)
        if grp:
            merged.append(call_llm("human_flavor",
                                   REDUCE_GROUP_PROMPT.format(notes="\n\n=====\n\n".join(grp)),
                                   timeout=1800))
        notes = merged
    return notes[0]


def _write(domain: str, body: str, sources: list[str], n: int) -> Path:
    links = "\n".join(f"- [[{s}]]" for s in sources)
    OUT_FILE.write_text(f"""---
type: 评论真人味基石
layer: 真人语感基石(跨账号·always-on·全量进 prompt)
status: 待审(reduce 生成 · 接进写手前先过目)
source: 爆款评论语感燃料 / {domain}（{n} 条爆款评论区 · 只取"说话方式(style)"层 map-reduce）
domain: {domain}
sample_n: {n}
created: {date.today()}
tags: [评论真人味基石, 真人痕迹, 语感]
---

# 评论真人味基石(跨账号·always-on)

> **这是什么**:从 {n} 条爆款视频【评论区】里提炼的"真人开口到底怎么说话"——正向范例,不是规则训诫。
> 它补的是 [[真人写作基石]] 缺的那块:真人写作基石是公众号【书面】文章,这份是观众【随口说】的话,口播找口语语感看它。
> **怎么用**:创作时和真人写作基石一起**全量加载**;靠这些真实评论原句的语感驱动文风,不靠堆禁令。
> **只含"怎么说话"层**(语气/口语/玩梗/打比方);"大家在聊什么痛点"是选题材料,留在 `vault/语感燃料/` 按选题检索,不在这里。

{body.strip()}

## 关联
- 书面那一半的地基:[[真人写作基石]]
- 料源(各爆款评论区语感燃料):
{links}
""", encoding="utf-8")
    return OUT_FILE


def main() -> None:
    p = argparse.ArgumentParser()
    p.add_argument("--domain", help="限定领域(默认燃料里第一个)")
    p.add_argument("--dry-run", action="store_true", help="抽清单+跑归纳,打印不落盘")
    p.add_argument("--force", action="store_true", help="成品已存在也重跑")
    p.add_argument("--status", action="store_true", help="看源与产出状态")
    a = p.parse_args()

    domains = _domains()
    if a.status:
        print(f"评论语感燃料根目录: {FUEL_DIR}")
        print(f"  领域: {domains or '(无)'}")
        for d in domains:
            blocks, srcs, n = _gather(d)
            print(f"  [{d}] 含 style 层的源文件 {n} 个")
        print(f"成品 {OUT_FILE.relative_to(ROOT)}: {'✓已存在' if OUT_FILE.exists() else '⬜未产'}")
        return

    if not domains:
        sys.exit(f"没有评论语感燃料: {FUEL_DIR}(先跑评论提炼产出 vault/语感燃料/提炼/爆款评论/)")
    domain = a.domain or domains[0]
    if OUT_FILE.exists() and not a.force and not a.dry_run:
        sys.exit(f"成品已存在: {OUT_FILE.relative_to(ROOT)}(--force 重跑 / --dry-run 预览)")

    blocks, sources, n = _gather(domain)
    if n == 0:
        sys.exit(f"[{domain}] 没抽到 style 层语感,检查燃料文件的 '## 分类 / style' 标。")
    _log(f"[{domain}] 抽到 {n} 个源的 style 层,开跑归纳(human_flavor=sonnet)。")
    try:
        body = _reduce(domain, blocks)
    except SessionLimitError as e:
        sys.exit(f"⏸ 撞会话上限,未产。{e.reset_at:%H:%M} 后重跑。")

    if a.dry_run:
        print(f"\n===== [{domain}] 抽到 {n} 源 · DRY-RUN(不落盘)=====")
        print(f"源: {', '.join(sources)}\n")
        print(body)
        return
    path = _write(domain, body, sources, n)
    _log(f"[{domain}] ✓ 评论真人味基石 → {path.relative_to(ROOT)}(归纳自 {n} 源)。审核后接进写手派任务卡。")


if __name__ == "__main__":
    main()
