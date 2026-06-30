"""范例升级(摊②·LLM 生成节点):把范例从「为什么有效」补成「可照做 + AI 反例」。

承接 reduce.py 产出的正向范例(范例本体=真台词 + 为什么有效)。本脚本**不动真台词**,
只给每张范例补两节:
  - AI 会怎么写砸(反例 foil):同意图换 AI 来写大概率塌成的呆版 + 一句露馅点。
  - 怎么照做:给写手一条动作级、能照搬的操作。

防编造安全闸(对齐 experience-library-reorg-plan 摊②):
  「范例本体」那句**真台词逐字锁死**=好例(来自原文爆款,非 LLM 编);LLM 只准造反例 + 照做,
  禁改、禁复述真台词。

幂等/续跑:已含「## 怎么照做」的范例视为已升级,跳过(--force 重升)。撞会话上限→干净中止续跑。
模型走 call.py 路由(reverse_reduce=sonnet),预算闸熔断同 reduce.py。

用法:
  python scripts/reverse/upgrade_examples.py --status              # 看各桶升级进度
  python scripts/reverse/upgrade_examples.py --dim 钩子 --dry-run   # 只跑钩子·打印不落盘(先看后放开)
  python scripts/reverse/upgrade_examples.py --dim 钩子             # 只跑钩子(落盘)
  python scripts/reverse/upgrade_examples.py --batch               # 跑/续:所有桶所有未升级范例
  python scripts/reverse/upgrade_examples.py --batch --strength-min 4   # 只升高强度(降成本)
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

VAULT_EXAMPLES = ROOT / "vault" / "范例"
LOG_FILE = ROOT / "logs" / "upgrade_examples.log"
DIMS = ["选题", "钩子", "结构"]   # 文风维度已移除(不再从爆款金句产范例)
DONE_MARK = "## 怎么照做"        # 已升级标记(幂等判据)

PROMPT = load_prompt("范例升级")  # 提示词搬进 .claude/skills/范例升级/SKILL.md


def _log(msg: str) -> None:
    line = f"{datetime.now():%m-%d %H:%M:%S} {msg}"
    print(line)
    try:
        LOG_FILE.parent.mkdir(parents=True, exist_ok=True)
        with LOG_FILE.open("a", encoding="utf-8") as fh:
            fh.write(line + "\n")
    except Exception:
        pass


def _split_frontmatter(text: str) -> tuple[str, str]:
    """返回 (frontmatter含两道---, body)。无 frontmatter 则 ('', text)。"""
    if not text.startswith("---"):
        return "", text
    end = text.find("\n---", 3)
    if end == -1:
        return "", text
    nl = text.find("\n", end + 1)
    return text[:nl + 1], text[nl + 1:]


def _section(body: str, name: str) -> str:
    """取 '## name' 段内容(到下一个 ## 或结尾)。"""
    m = re.search(rf"^##\s+{re.escape(name)}\s*$", body, re.MULTILINE)
    if not m:
        return ""
    start = m.end()
    nxt = re.search(r"^##\s+", body[start:], re.MULTILINE)
    return body[start:start + nxt.start()].strip() if nxt else body[start:].strip()


def _parse_llm(out: str) -> dict | None:
    """LLM 原文 → {呆版, 露馅, 照做};缺关键字段返回 None。"""
    fan = re.search(r"===反例===(.*?)===照做===", out, re.DOTALL)
    zuo = re.search(r"===照做===(.*)$", out, re.DOTALL)
    if not fan or not zuo:
        return None
    fb = fan.group(1)
    dai = re.search(r"呆版\s*[:：]\s*(.*)", fb)
    lou = re.search(r"露馅\s*[:：]\s*(.*)", fb)
    zhao = zuo.group(1).strip()
    if not (dai and lou and zhao):
        return None
    return {"呆版": dai.group(1).strip(), "露馅": lou.group(1).strip(), "照做": zhao}


def _upgrade_text(text: str, parts: dict) -> str:
    """把反例/照做两节插到「## 关联」前(无关联则末尾追加),并给 frontmatter 标 upgraded。"""
    fm, body = _split_frontmatter(text)
    if "upgraded:" not in fm:
        fm = fm.rstrip("\n")
        fm = fm[:-3] + f"upgraded: {date.today()}\n---\n" if fm.endswith("---\n") or fm.endswith("---") \
            else fm + "\n"
    block = (f"\n## AI 会怎么写砸(反例)\n"
             f"> 呆版:{parts['呆版']}\n"
             f"> 露馅:{parts['露馅']}\n\n"
             f"{DONE_MARK}\n{parts['照做']}\n")
    m = re.search(r"^##\s+关联\s*$", body, re.MULTILINE)
    if m:
        new_body = body[:m.start()].rstrip() + "\n" + block + "\n" + body[m.start():]
    else:
        new_body = body.rstrip() + "\n" + block
    return fm + new_body


def _cards(dim: str, strength_min: int):
    folder = VAULT_EXAMPLES / dim
    if not folder.exists():
        return []
    out = []
    for p in sorted(folder.glob("*.md")):
        text = p.read_text(encoding="utf-8")
        sm = re.search(r"strength:\s*(\d+)", text)
        s = int(sm.group(1)) if sm else 0
        out.append((p, text, s))
    return [(p, t, s) for p, t, s in out if s >= strength_min]


def run_dim(dim: str, strength_min: int, dry_run: bool, force: bool) -> int:
    cards = _cards(dim, strength_min)
    done = 0
    for path, text, s in cards:
        if not force and DONE_MARK in text:
            continue
        body = _split_frontmatter(text)[1]
        ben = _section(body, "范例本体")
        why = _section(body, "为什么有效")
        if not ben:
            _log(f"[{dim}] {path.name} 无「范例本体」,跳过。")
            continue
        out = call_llm("reverse_reduce", PROMPT.format(dim=dim, body=ben, why=why or "(未写)"))
        parts = _parse_llm(out)
        if not parts:
            _log(f"⚠️ [{dim}] {path.name} LLM 输出没按格式,跳过(可重跑)。")
            continue
        if dry_run:
            print(f"\n===== [{dim}] {path.name} (DRY-RUN) =====")
            print(f"真台词(锁死):{ben[:80]}")
            print(f"  呆版反例:{parts['呆版']}")
            print(f"  露馅:{parts['露馅']}")
            print(f"  怎么照做:{parts['照做']}")
        else:
            path.write_text(_upgrade_text(text, parts), encoding="utf-8")
        done += 1
    _log(f"[{dim}] {'DRY-RUN ' if dry_run else ''}升级 {done} 张(强度≥{strength_min})。")
    return done


def run_batch(strength_min: int, force: bool) -> None:
    total = 0
    for dim in DIMS:
        try:
            total += run_dim(dim, strength_min, dry_run=False, force=force)
        except SessionLimitError as e:
            _log(f"⏸ 撞订阅会话上限:[{dim}] 中止(已升级的已落盘·幂等不重升)。"
                 f"{e.reset_at:%H:%M} 重置后跑 `--batch` 自动续。")
            return
    _log(f"✅ 范例升级完成,共升级 {total} 张。")


def status() -> None:
    for dim in DIMS:
        folder = VAULT_EXAMPLES / dim
        files = sorted(folder.glob("*.md")) if folder.exists() else []
        up = sum(1 for p in files if DONE_MARK in p.read_text(encoding="utf-8"))
        print(f"  {dim}: 已升级 {up}/{len(files)} 张")


def main() -> None:
    p = argparse.ArgumentParser()
    g = p.add_mutually_exclusive_group(required=True)
    g.add_argument("--batch", action="store_true", help="跑/续:所有桶未升级范例")
    g.add_argument("--dim", choices=DIMS, help="只跑一个桶")
    g.add_argument("--status", action="store_true", help="看进度")
    p.add_argument("--strength-min", type=int, default=0, help="只升强度≥此值(降成本)")
    p.add_argument("--dry-run", action="store_true", help="配 --dim:只打印不落盘")
    p.add_argument("--force", action="store_true", help="已升级的也重升")
    a = p.parse_args()
    if a.status:
        status()
    elif a.dim:
        try:
            run_dim(a.dim, a.strength_min, a.dry_run, a.force)
        except SessionLimitError as e:
            sys.exit(f"⏸ 撞会话上限,未完。{e.reset_at:%H:%M} 后再跑。")
    else:
        run_batch(a.strength_min, a.force)


if __name__ == "__main__":
    main()
