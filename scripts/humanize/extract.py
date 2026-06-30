"""真人写作提炼工作器(长期任务·随时启停·断点续跑)。

跨人类范文找"人到底是怎么写的"共性 → 真人写作范例(正向范例,非规则禁令)。
体量大(736篇/~373万字,塞不进一次上下文)→ map-reduce:
  map:    范文按字符预算分块,每块提炼"真人写作痕迹"笔记(原句为证),逐块落盘 checkpoint。
  reduce: 把各块笔记按预算分组归并(必要时多级)→ 最终真人写作范例。

随时启停:你睡觉/出门时跑 `--map`,它一块一块消化;
  - 撞订阅会话上限 → 干净中止,记录进度,重置后跑同命令从断点续;
  - Ctrl+C → 当前块跑完的已落盘,下次续;
  - 完成的块靠 manifest 永不重跑(不重读 = 无死循环)。

用法:
  python scripts/humanize/extract.py --plan     # 只规划分块、看要跑多少块,不调 LLM
  python scripts/humanize/extract.py --map       # 跑/续 map(可反复跑直到全完成)
  python scripts/humanize/extract.py --reduce    # 全部 map 完成后,归并成最终真人写作范例
  python scripts/humanize/extract.py --status     # 看进度
"""
import argparse
import json
import os
import sys
import time
from datetime import datetime
from pathlib import Path

import yaml

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "scripts" / "llm"))
sys.path.insert(0, str(ROOT / "scripts" / "feishu"))
sys.stdout.reconfigure(encoding="utf-8", errors="replace")
from call import call_llm, load_prompt, SessionLimitError  # noqa: E402


LOG_FILE = ROOT / "logs" / "humanize_map.log"


def _log(msg: str) -> None:
    """进度落本地日志(不推飞书):回来看 logs/humanize_map.log 即是记录。"""
    line = f"{datetime.now():%m-%d %H:%M:%S} {msg}"
    print(line)
    try:
        LOG_FILE.parent.mkdir(parents=True, exist_ok=True)
        with LOG_FILE.open("a", encoding="utf-8") as fh:
            fh.write(line + "\n")
    except Exception:
        pass

CFG = yaml.safe_load((ROOT / "config" / "settings.yaml").read_text(encoding="utf-8"))["humanize"]
CORPUS_DIR = Path(CFG["corpus_dir"])
CHUNK_CHARS = CFG["chunk_chars"]
REDUCE_CHARS = CFG["reduce_chars"]
OUT_DIR = ROOT / CFG["out_dir"]
MAP_DIR = OUT_DIR / "map"
MANIFEST = OUT_DIR / "manifest.json"
LOCK = OUT_DIR / ".map.running"      # 防 bat 与飞书触发并发跑同一 manifest
LOCK_STALE_SEC = 900                 # 锁超 15 分钟没刷新 = 上次崩了,视为失效

MAP_PROMPT = load_prompt("真人写作基石", "map")       # 提示词搬进 .claude/skills/真人写作基石/SKILL.md
REDUCE_PROMPT = load_prompt("真人写作基石", "reduce")


def _load_manifest() -> dict | None:
    if MANIFEST.exists():
        return json.loads(MANIFEST.read_text(encoding="utf-8"))
    return None


def _save_manifest(m: dict) -> None:
    OUT_DIR.mkdir(parents=True, exist_ok=True)
    MANIFEST.write_text(json.dumps(m, ensure_ascii=False, indent=2), encoding="utf-8")


def plan() -> dict:
    """确定性分块:范文按作者轮转交错排序后贪心装进字符预算。已有 manifest 则复用(保证续跑稳定)。

    目标=提炼跨所有人的通用真人写作痕迹,所以每块尽量横跨多位作者(轮转交错),
    让 MAP 阶段每块就在做跨作者横向对比、共性更早浮现。
    注意:作者篇数极不均(150 vs 5)→ 小作者会先被取空,靠后的块只剩大作者,属正常。
    """
    existing = _load_manifest()
    if existing:
        return existing
    if not CORPUS_DIR.exists():
        sys.exit(f"语料目录不存在: {CORPUS_DIR}")
    files = sorted(p for p in CORPUS_DIR.rglob("*.md") if p.is_file())
    if not files:
        sys.exit(f"语料目录没有 .md: {CORPUS_DIR}")
    # 按作者(相对路径首段)分组,组内按名排序
    by_author: dict[str, list[Path]] = {}
    for f in files:
        rel = f.relative_to(CORPUS_DIR)
        author = rel.parts[0] if len(rel.parts) > 1 else "_顶层"
        by_author.setdefault(author, []).append(f)
    # 轮转交错:每轮从每个作者各取一篇 → 块内天然横跨所有作者
    queues = [list(v) for v in by_author.values()]
    ordered: list[Path] = []
    while any(queues):
        for q in queues:
            if q:
                ordered.append(q.pop(0))
    chunks, cur, cur_chars = [], [], 0
    for f in ordered:
        n = len(f.read_text(encoding="utf-8", errors="replace"))
        if cur and cur_chars + n > CHUNK_CHARS:        # 超预算 → 收口当前块
            chunks.append(cur)
            cur, cur_chars = [], 0
        cur.append(str(f.relative_to(CORPUS_DIR)))     # 相对路径(含作者子目录),防重名
        cur_chars += n
    if cur:
        chunks.append(cur)
    # 尾巴收口(轮转后只剩零头):
    #  - 末块远小于预算(多篇零头)→ 并进前一块,别为几篇单跑一次没对比的 map;
    #  - 末块只剩孤零一篇 → 丢弃:单作者、无跨作者对比,不要它(用户 2026-06-14 定)。
    if len(chunks) > 1:
        tail_chars = sum(len((CORPUS_DIR / f).read_text(encoding="utf-8", errors="replace"))
                         for f in chunks[-1])
        if tail_chars < CHUNK_CHARS * 0.3:
            chunks[-2].extend(chunks.pop())
        elif len(chunks[-1]) == 1:
            chunks.pop()
    m = {"corpus_dir": str(CORPUS_DIR), "chunk_chars": CHUNK_CHARS,
         "created": datetime.now().isoformat(timespec="seconds"),
         "chunks": [{"idx": i, "files": c, "status": "pending", "note": None}
                    for i, c in enumerate(chunks)]}
    _save_manifest(m)
    return m


def _chunk_text(files: list[str]) -> str:
    parts = []
    for name in files:
        body = (CORPUS_DIR / name).read_text(encoding="utf-8", errors="replace").strip()
        parts.append(f"--- 文章:{name} ---\n{body}")
    return "\n\n".join(parts)


def _acquire_lock() -> bool:
    if LOCK.exists() and time.time() - LOCK.stat().st_mtime < LOCK_STALE_SEC:
        return False
    OUT_DIR.mkdir(parents=True, exist_ok=True)
    LOCK.write_text(str(os.getpid()))
    return True


def run_map() -> None:
    m = plan()
    MAP_DIR.mkdir(parents=True, exist_ok=True)
    total = len(m["chunks"])
    pending = [c for c in m["chunks"] if c["status"] != "done"]
    if not pending:
        print(f"map 全部完成({total} 块)。下一步: --reduce")
        return
    if not _acquire_lock():
        print("已有一个 map 在跑(锁未释放)。确认没在跑可删 data/humanize/.map.running 再试。")
        return
    done_at_start = total - len(pending)        # 本轮起跑前已完成数,用于汇报「本轮新增」
    print(f"map: 共 {total} 块,待跑 {len(pending)} 块。撞限或 Ctrl+C 可随时停,跑同命令续。")
    try:
        for c in pending:
            try:
                note = call_llm("human_flavor", MAP_PROMPT.format(
                    n=len(c["files"]), articles=_chunk_text(c["files"])), timeout=1800)
            except SessionLimitError as e:
                done = sum(1 for x in m["chunks"] if x["status"] == "done")
                gained = done - done_at_start
                _log(f"⏸ 撞订阅会话上限:本轮存 {gained} 块,累计 {done}/{total}(已落盘·永不重跑),"
                     f"还剩 {total - done} 块。{e.reset_at:%H:%M} 重置后跑 `--map` 自动续。")
                return
            except KeyboardInterrupt:
                done = sum(1 for x in m["chunks"] if x["status"] == "done")
                gained = done - done_at_start
                _log(f"⏸ 手动中断:本轮存 {gained} 块,累计 {done}/{total}(当前块未计入·已落盘永不重跑),"
                     f"还剩 {total - done} 块。跑 `--map` 续。")
                return
            path = MAP_DIR / f"chunk_{c['idx']:03d}.md"
            path.write_text(note, encoding="utf-8")
            c["status"], c["note"] = "done", str(path.relative_to(ROOT))
            _save_manifest(m)                          # 每块即时落盘:中途死也不丢
            LOCK.write_text(str(os.getpid()))          # 刷新锁 mtime,表明还活着
            done = sum(1 for x in m["chunks"] if x["status"] == "done")
            _log(f"✓ 块 {c['idx']:03d}({len(c['files'])}篇)→ {path.name}  累计 {done}/{total} 已落盘。")
    finally:
        LOCK.unlink(missing_ok=True)
    _log(f"✅ map 全部完成({total} 块)。下一步跑 `--reduce` 归并成最终范例。")


def _reduce_once(notes: list[str]) -> list[str]:
    """把笔记按 reduce 预算分组,每组归并一次,返回归并后的笔记(可能仍 >1,需再来一轮)。"""
    groups, cur, cur_chars = [], [], 0
    for nt in notes:
        if cur and cur_chars + len(nt) > REDUCE_CHARS:
            groups.append(cur)
            cur, cur_chars = [], 0
        cur.append(nt)
        cur_chars += len(nt)
    if cur:
        groups.append(cur)
    out = []
    for i, g in enumerate(groups):
        joined = "\n\n=====\n\n".join(g)
        out.append(call_llm("human_flavor", REDUCE_PROMPT.format(notes=joined), timeout=1800))
        print(f"  ✓ 归并组 {i + 1}/{len(groups)}({len(g)} 份)")
    return out


def run_reduce() -> None:
    m = _load_manifest()
    if not m:
        sys.exit("还没 plan/map。先跑 --map。")
    pending = [c for c in m["chunks"] if c["status"] != "done"]
    if pending:
        sys.exit(f"还有 {len(pending)} 块没 map 完,先把 --map 跑完再 reduce。")
    notes = [(ROOT / c["note"]).read_text(encoding="utf-8") for c in m["chunks"]]
    print(f"reduce: {len(notes)} 份块笔记,按 {REDUCE_CHARS} 字预算多级归并 …")
    try:
        level = 0
        while len(notes) > 1:
            level += 1
            print(f"第 {level} 级归并({len(notes)} 份)…")
            notes = _reduce_once(notes)
        if len(notes) > 1 or not notes:
            sys.exit("归并异常")
    except SessionLimitError as e:
        print(f"\n⏸ 撞会话上限,reduce 未完成。{e}\n   重置后重跑 `--reduce`(map 块笔记已在,不重读语料)。")
        return
    out = OUT_DIR / f"真人写作提炼_{datetime.now():%Y%m%d}.md"
    out.write_text(f"# 真人写作范例(提炼自 {CORPUS_DIR.name})\n"
                   f"> 来源:{len(m['chunks'])} 块 / {sum(len(c['files']) for c in m['chunks'])} 篇人类范文\n"
                   f"> 生成:{datetime.now():%Y-%m-%d}\n\n{notes[0]}\n", encoding="utf-8")
    print(f"\n✅ 最终真人写作范例: {out.relative_to(ROOT)}\n   审核后晋升进成品仓(vault 真人写作基石)。")


def status() -> None:
    m = _load_manifest()
    if not m:
        print("尚未规划。跑 --plan 或 --map。")
        return
    done = sum(1 for c in m["chunks"] if c["status"] == "done")
    total = len(m["chunks"])
    files = sum(len(c["files"]) for c in m["chunks"])
    print(f"语料: {m['corpus_dir']}\nmap 进度: {done}/{total} 块完成(共 {files} 篇)")
    if done < total:
        print(f"剩 {total - done} 块,跑 --map 续。")
    else:
        print("map 全完成,可 --reduce。")


def main() -> None:
    p = argparse.ArgumentParser()
    g = p.add_mutually_exclusive_group(required=True)
    g.add_argument("--plan", action="store_true", help="只规划分块,不调 LLM")
    g.add_argument("--map", action="store_true", help="跑/续 map 提炼")
    g.add_argument("--reduce", action="store_true", help="归并成最终真人写作范例")
    g.add_argument("--status", action="store_true", help="看进度")
    a = p.parse_args()
    if a.plan:
        m = plan()
        files = sum(len(c["files"]) for c in m["chunks"])
        print(f"语料 {m['corpus_dir']}\n规划: {files} 篇 → {len(m['chunks'])} 块"
              f"(每块预算 {CHUNK_CHARS} 字)。跑 --map 开始消化。")
    elif a.map:
        run_map()
    elif a.reduce:
        run_reduce()
    else:
        status()


if __name__ == "__main__":
    main()
