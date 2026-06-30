"""逆向归并共性(reduce·LLM 生成节点):多篇 DNA 拆解笔记 → 领域层范例 + 方法论路由表。

承接 dna.py:dna.py 是 map(逐条单篇拆 DNA,产中间燃料 data/爆款拆解/);
本脚本是 reduce(把同领域多篇 DNA 归纳出共性)。产两类**成品**(对齐 extraction-output-spec):
  1. 范例 → vault/范例/{钩子,结构,选题}/   —— 少而厚,**唯一进 prompt 的东西**
  2. 方法论路由表 → vault/方法论/{维度}打法路由表.md  —— 做"选哪类打法"的路由,**不进 prompt**

文风**不在这里产**:爆款金句多半也是 AI 写的,拿它归纳文风=把 AI 味的精华句当正向范例,
反而加重 AI 味(支柱"正向范例驱动文风"被反着用)。文风只认两个干净料源——
真人写作基石(vault/真人写作基石.md·always-on)+ 评论语感燃料(vault/语感燃料/)。别把"文风"加回 DIMS。

按"维度"分批,一个维度一趟 LLM(沿用 call.py 撞限熔断+断点续):
  钩子 / 结构 / 选题。每趟:
  ① 代码先抽要点(不耗 LLM):把同领域所有 DNA 的该维度段落 + 标题 + 超额倍数抽成清单;
     钩子维度额外从口播文案切"前3秒开篇窗口"(确定性),让归纳落在真实开篇上,不靠二手转述。
  ② sonnet 归纳:清单 → 几类打法,每类挑 2-3 个最强(按超额倍数=强度)样例 → 路由表 + 范例。

守硬规则:超额倍数=强度只晋高强度;每类打法范例 ≤MAX_PER_PLAY;范例盖来源+时间戳留待淘汰;
洗稿/二创已被 hits.cluster_id 去重(只对代表作)。

幂等/续跑:维度=断点单元。某维度路由表已存在即视为已跑,跳过(--force 重跑)。撞会话上限→干净中止。

用法:
  python scripts/reverse/reduce.py --status                 # 看各维度进度
  python scripts/reverse/reduce.py --dim 钩子 --dry-run      # 只跑钩子、打印产出不落盘(先看后放开)
  python scripts/reverse/reduce.py --dim 钩子                # 只跑钩子(落盘)
  python scripts/reverse/reduce.py --batch                  # 跑/续:所有未跑的维度
  python scripts/reverse/reduce.py --domain 泛科普 ...        # 限定领域(默认全领域分组各跑)
"""
import argparse
import re
import sys
from datetime import date, datetime
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "scripts" / "collect"))
sys.path.insert(0, str(ROOT / "scripts" / "llm"))
sys.stdout.reconfigure(encoding="utf-8", errors="replace")
from common import connect, safe_title  # noqa: E402
from call import call_llm, load_prompt, SessionLimitError  # noqa: E402

VAULT_EXAMPLES = ROOT / "vault" / "范例"
VAULT_METHOD = ROOT / "vault" / "方法论"
LOG_FILE = ROOT / "logs" / "reduce_batch.log"

MAX_PER_PLAY = 3        # 每类打法范例上限(少而厚)
SECTION_CAP = 1200      # 单篇单段喂给 LLM 的字数上限(控 token)
OPENING_TARGET = 50     # 开篇窗口目标字数(口播"前3秒"约此量)
OPENING_MAX_SENT = 3    # 开篇窗口最多取几句

# 维度配置:文件夹 / 在 DNA 笔记里对应哪个 ## 段(按 header 前缀匹配)/ 是否需要口播开篇 / 归纳焦点
DIMS = {
    "结构": {"section": "结构", "opening": False,
             "focus": "全片结构骨架——起承转合的节拍套路、信息怎么递进"},
    "选题": {"section": "选题", "opening": False,
             "focus": "选题角度——做什么题、信息差在哪、为谁、谁愿意转发"},
    # 钩子维度已移除(2026-06-23):A/B 证伪"挖爆点→查路由表配招"框架。钩子方法论+例子改用
    #   手搓的 vault/方法论/钩子打法.md(留存优先·老 compose 框架),reduce 不再产钩子路由表/范例卡。
    # 文风维度已移除(原从爆款金句归纳=AI味污染源)。文风走 真人写作基石 + 评论语感燃料,不在此产。
}

# 维度 → 配套"怎么做"手写操作笔记(路由表指向它,管公式/骨架/自检)
# 钩子已移除:钩子方法论改用手搓的老框架(vault/方法论/钩子打法.md·留存优先),reduce 不再产它的路由表/怎么做。
COMPANION = {"结构": "结构打法·怎么做"}

# 「选题判断维度」:不同于上面的类型路由表(类型路由表只读「选题」段、产类型分类)。
# 这一档归纳的是"好选题反复踩中哪几个判断维度"——料散在多段(选题/张力/共鸣转发),
# 喂多段一起 reduce,产一份判断尺(每领域一份),给选题延展/创作判断"一个新选题值不值得做"用。
# 维度从爆款长出、领域不同维度不同,不手写。旧笔记缺共鸣段也无妨(选题+张力即够出第一版)。
CRITERIA_DIM = "选题判断维度"
CRITERIA_SECTIONS = ["选题", "张力", "共鸣与转发机制"]  # 料源段(按前缀匹配·缺的跳过)

CRITERIA_PROMPT = load_prompt("归纳", "判断维度")  # 提示词搬进 .claude/skills/归纳/SKILL.md

# LLM 归纳 prompt(搬进 skill;LLM 只在轨道里归纳,不决定流程)。输出契约见 skill,代码据此切分落盘。
PROMPT = load_prompt("归纳", "路由表")

# 各维度范例"本体"该放什么 + 可选标签词表(来自 vault/词表/分类词表.md)
BODY_HINT = {
    "钩子": "开篇原句", "结构": "结构骨架(分节拍列出)",
    "选题": "选题的一句话表述 + 信息差",
}
TAG_VOCAB = {
    "钩子": "悬念提问/反常识断言/数字冲击/场景代入/利益承诺/威胁警告/身份点名/对比反差",
    "结构": "总分总/悬念递进/三段列举/故事弧/问题-原理-应用/辟谣-真相-启示",
    "选题": "冷知识/反常识/热点解读/身边科学/历史揭秘/健康辟谣/硬核原理/社会现象",
}

_SENT_END = re.compile(r"[。！？!?…]+")


def _log(msg: str) -> None:
    line = f"{datetime.now():%m-%d %H:%M:%S} {msg}"
    print(line)
    try:
        LOG_FILE.parent.mkdir(parents=True, exist_ok=True)
        with LOG_FILE.open("a", encoding="utf-8") as fh:
            fh.write(line + "\n")
    except Exception:
        pass


def _strength(excess) -> int:
    """超额倍数 → 1-5 强度(范例 frontmatter)。"""
    e = excess or 0
    return 5 if e >= 6 else 4 if e >= 4 else 3 if e >= 2.5 else 2 if e >= 1.8 else 1


def _parse_sections(note_text: str) -> dict:
    """DNA 笔记正文 → {去掉## 的段标题: 段内容}。按 '## ' 行切。"""
    sections, cur, buf = {}, None, []
    for line in note_text.splitlines():
        if line.startswith("## "):
            if cur is not None:
                sections[cur] = "\n".join(buf).strip()
            cur, buf = line[3:].strip(), []
        elif cur is not None:
            buf.append(line)
    if cur is not None:
        sections[cur] = "\n".join(buf).strip()
    return sections


def _section_for(sections: dict, prefix: str) -> str:
    """取标题以 prefix 开头的段(避开'可裂变选题'误配'选题')。"""
    for head, body in sections.items():
        if head.startswith(prefix):
            return body
    return ""


def _opening_window(transcript: str) -> str:
    """口播文案 → 前3秒开篇窗口(确定性):累计句子到约 OPENING_TARGET 字或 OPENING_MAX_SENT 句,至少1句。"""
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
        if (i + 1) >= 1 and (len(out) >= OPENING_TARGET or (i + 1) >= OPENING_MAX_SENT):
            break
    return out.strip()


def _gather(conn, dim: str, domain: str):
    """抽某领域所有已拆 DNA 的该维度要点(不耗 LLM)。返回 (digest_str, hit_rows_by_id)。"""
    cfg = DIMS[dim]
    rows = conn.execute(
        "SELECT h.*, c.domain AS domain, c.name AS competitor "
        "FROM hits h JOIN competitor_accounts c ON c.id=h.competitor_id "
        "WHERE h.dna_note_path IS NOT NULL AND h.dna_note_path!='' AND c.domain=? "
        "ORDER BY h.excess_ratio DESC", (domain,)).fetchall()
    lines, by_id = [], {}
    for h in rows:
        note = ROOT / h["dna_note_path"]
        if not note.exists():
            continue
        secs = _parse_sections(note.read_text(encoding="utf-8"))
        body = _section_for(secs, cfg["section"])[:SECTION_CAP]
        if not body:
            continue
        by_id[h["id"]] = h
        block = [f"[hit {h['id']} | 超额{h['excess_ratio']}x] 标题:{h['title']}"]
        if cfg["opening"] and h["transcript_path"]:
            tp = ROOT / h["transcript_path"]
            if tp.exists():
                block.append(f"  开篇原文(前3秒):{_opening_window(tp.read_text(encoding='utf-8'))}")
        block.append(f"  DNA-{dim}:{body}")
        lines.append("\n".join(block))
    return "\n\n".join(lines), by_id


# ---- 切分 LLM 输出 → 路由表 + 范例块 ----
_FIELD_KEYS = ["hit_id", "打法", "标签", "点题", "本体", "为什么有效"]
_FIELD_RE = re.compile(r"^(" + "|".join(_FIELD_KEYS) + r")\s*[:：]\s*(.*)$")


def _split_output(text: str):
    """LLM 原文 → (路由表正文, [范例字段dict,...])。"""
    chunks = text.split("===范例===")
    method = chunks[0].strip()
    cards = []
    for chunk in chunks[1:]:
        fields, cur = {}, None
        for line in chunk.splitlines():
            m = _FIELD_RE.match(line.strip())
            if m:
                cur = m.group(1)
                fields[cur] = m.group(2).strip()
            elif cur and line.strip():           # 续行(本体/为什么有效可能多行)
                fields[cur] += "\n" + line.strip()
        if fields.get("hit_id"):
            cards.append(fields)
    return method, cards


def _cited_hits(table_text: str, valid_ids) -> list:
    """从 markdown 路由表「代表 hit」列(每行最后一格)抽引用到的 hit 编号,保序去重。
    只读最后一列,避开触发条件里的数字(如 900亿/5万家)误当编号。"""
    seen, out = set(), []
    for line in table_text.splitlines():
        if "|" not in line:
            continue
        cells = [c.strip() for c in line.strip().strip("|").split("|")]
        if not cells:
            continue
        last = cells[-1]
        if "代表" in last or set(last) <= set("-: "):   # 表头/分隔行
            continue
        for m in re.finditer(r"\d+", last):
            hid = int(m.group())
            if hid in valid_ids and hid not in seen:
                seen.add(hid)
                out.append(hid)
    return out


def _write_method(dim: str, domain: str, method_body: str, n: int, by_id: dict) -> Path:
    VAULT_METHOD.mkdir(parents=True, exist_ok=True)
    path = VAULT_METHOD / f"{dim}打法路由表.md"
    # 关联爆款:把「代表 hit」列连成 [[ ]],接通 方法论 ↔ 来源拆解(Obsidian 自动反链)
    links = [f"- [[{Path(by_id[hid]['dna_note_path']).stem}]](hit {hid})"
             for hid in _cited_hits(method_body, set(by_id))]
    footer = "\n## 关联爆款(点进去看完整拆解)\n" + "\n".join(links) + "\n" if links else ""
    howto = f"\n> ▶ 每招**怎么做**(公式/骨架/自检)见 [[{COMPANION[dim]}]]" if dim in COMPANION else ""
    path.write_text(f"""---
type: 方法论·路由表
bucket: {dim}
domain: {domain}
source: competitor_hit_reduce
sample_n: {n}
status: active
created: {date.today()}
usage: 不整篇进prompt。创作时按选题特征匹配打法 → 取该打法指向的范例注入
tags: [方法论, {dim}]
---

# {dim}打法 · 路由表({domain})

> 由 {n} 条已验证爆款 DNA 归纳(reduce)。**用法**:按选题/素材特征匹配打法 → 去 `范例/{dim}/` 取该打法对应范例注入,不整篇进 prompt。{howto}

{method_body}
{footer}""", encoding="utf-8")
    return path


def _write_card(dim: str, fields: dict, h) -> Path | None:
    folder = VAULT_EXAMPLES / dim
    folder.mkdir(parents=True, exist_ok=True)
    slug = safe_title(fields.get("点题") or h["title"], h["id"])
    path = folder / f"{slug}_{h['id']}.md"
    tags = [t.strip() for t in re.split(r"[,，、/]", fields.get("标签", "")) if t.strip()]
    path.write_text(f"""---
type: {dim}
domain: {h['domain']}
account:
strength: {_strength(h['excess_ratio'])}
source: competitor_hit
source_id: {h['id']}
play: "{fields.get('打法', '').replace('"', '')}"
tags: {tags}
status: active
created: {date.today()}
last_used:
---

# {fields.get('点题', '').strip()}

## 范例本体
{fields.get('本体', '').strip()}

## 为什么有效
{fields.get('为什么有效', '').strip()}

## 关联
- 来源爆款拆解:[[{Path(h['dna_note_path']).stem}]](hit {h['id']}·超额 {h['excess_ratio']}x)
- 打法路由:[[{dim}打法路由表]]
""", encoding="utf-8")
    return path


def run_dim(conn, dim: str, domain: str, dry_run: bool = False) -> int:
    """跑一个维度。返回写出的范例数(dry_run 返回计划数)。撞限抛 SessionLimitError 不吞。"""
    cfg = DIMS[dim]
    digest, by_id = _gather(conn, dim, domain)
    n = len(by_id)
    if n == 0:
        _log(f"[{domain}/{dim}] 无可归纳的 DNA 段,跳过。")
        return 0
    prompt = PROMPT.format(domain=domain, dim=dim, n=n, focus=cfg["focus"],
                           digest=digest, tags=TAG_VOCAB[dim],
                           body_hint=BODY_HINT[dim])
    out = call_llm("reverse_reduce", prompt)
    method_body, cards = _split_output(out)

    # 限量:每类打法 ≤ MAX_PER_PLAY,按强度(超额)排
    cards = [c for c in cards if int(re.sub(r"\D", "", c["hit_id"]) or 0) in by_id]
    cards.sort(key=lambda c: by_id[int(re.sub(r"\D", "", c["hit_id"]))]["excess_ratio"] or 0,
               reverse=True)
    kept, per_play = [], {}
    for c in cards:
        play = c.get("打法", "")
        if per_play.get(play, 0) >= MAX_PER_PLAY:
            continue
        per_play[play] = per_play.get(play, 0) + 1
        kept.append(c)

    if dry_run:
        print(f"\n========== [{domain}/{dim}] DRY-RUN(不落盘) ==========")
        print("---- 方法论路由表 ----\n" + method_body)
        print(f"\n---- 范例 {len(kept)} 张(原 {len(cards)},按每类≤{MAX_PER_PLAY}限量后) ----")
        for c in kept:
            h = by_id[int(re.sub(r"\D", "", c["hit_id"]))]
            print(f"\n[hit {h['id']} · 强度{_strength(h['excess_ratio'])} · {c.get('打法','')}]"
                  f"\n  点题:{c.get('点题','')}\n  本体:{c.get('本体','')[:120]}...")
        return len(kept)

    mp = _write_method(dim, domain, method_body, n, by_id)
    written = 0
    for c in kept:
        h = by_id[int(re.sub(r"\D", "", c["hit_id"]))]
        if _write_card(dim, c, h):
            written += 1
    _log(f"[{domain}/{dim}] ✓ 路由表 {mp.name} + 范例 {written} 张(归纳自 {n} 条DNA)。")
    return written


# ---- 选题判断维度(多段料源 → 一份判断尺)----
def _gather_criteria(conn, domain: str):
    """抽某领域所有已拆 DNA 的「选题+张力+共鸣」段拼清单(不耗 LLM)。返回 (digest, by_id)。"""
    rows = conn.execute(
        "SELECT h.*, c.domain AS domain, c.name AS competitor "
        "FROM hits h JOIN competitor_accounts c ON c.id=h.competitor_id "
        "WHERE h.dna_note_path IS NOT NULL AND h.dna_note_path!='' AND c.domain=? "
        "ORDER BY h.excess_ratio DESC", (domain,)).fetchall()
    lines, by_id = [], {}
    for h in rows:
        note = ROOT / h["dna_note_path"]
        if not note.exists():
            continue
        secs = _parse_sections(note.read_text(encoding="utf-8"))
        parts = []
        for name in CRITERIA_SECTIONS:
            body = _section_for(secs, name)[:SECTION_CAP]
            if body:
                parts.append(f"  【{name}】{body}")
        if not parts:                       # 选题/张力/共鸣全缺,跳过
            continue
        by_id[h["id"]] = h
        lines.append("\n".join([f"[hit {h['id']} | 超额{h['excess_ratio']}x] 标题:{h['title']}"] + parts))
    return "\n\n".join(lines), by_id


def _criteria_links(body: str, by_id: dict) -> str:
    """从判断维度正文里的 'hit N' 引用(非表格,bullet 形式)接 [[来源拆解]] 反链,保序去重。"""
    seen, out = set(), []
    for m in re.finditer(r"hit\s*(\d+)", body, re.I):
        hid = int(m.group(1))
        if hid in by_id and hid not in seen:
            seen.add(hid)
            out.append(f"- [[{Path(by_id[hid]['dna_note_path']).stem}]](hit {hid})")
    return "\n## 关联爆款(点进去看完整拆解)\n" + "\n".join(out) + "\n" if out else ""


def _write_criteria(domain: str, body: str, n: int, by_id: dict) -> Path:
    VAULT_METHOD.mkdir(parents=True, exist_ok=True)
    path = VAULT_METHOD / "选题判断维度.md"
    path.write_text(f"""---
type: 方法论·判断维度
bucket: 选题
domain: {domain}
source: competitor_hit_reduce
sample_n: {n}
status: active
created: {date.today()}
usage: 选题延展/创作判断"一个选题值不值得做"时,逐维看它强不强;维度从该领域爆款归纳,不手写、不进prompt整篇
tags: [方法论, 选题, 判断维度]
---

# 选题判断维度 · {domain}

> 由 {n} 条已验证爆款的「选题/张力/共鸣」拆解归纳(reduce)。**用法**:判断一个新选题/衍生方向值不值得做时,逐维看它够不够强;维度从该领域爆款长出、领域不同维度不同,不手写。

{body}
{_criteria_links(body, by_id)}""", encoding="utf-8")
    return path


def run_criteria(conn, domain: str, dry_run: bool = False) -> Path | None:
    """归纳一份「选题判断维度」。撞限抛 SessionLimitError 不吞。"""
    digest, by_id = _gather_criteria(conn, domain)
    n = len(by_id)
    if n == 0:
        _log(f"[{domain}/{CRITERIA_DIM}] 无可归纳的 DNA 段,跳过。")
        return None
    out = call_llm("reverse_reduce", CRITERIA_PROMPT.format(domain=domain, n=n, digest=digest))
    if dry_run:
        print(f"\n========== [{domain}/{CRITERIA_DIM}] DRY-RUN(不落盘·归纳自 {n} 条DNA) ==========\n{out}")
        return None
    path = _write_criteria(domain, out.strip(), n, by_id)
    _log(f"[{domain}/{CRITERIA_DIM}] ✓ {path.name}(归纳自 {n} 条DNA)。")
    return path


def _domains(conn, only: str | None):
    if only:
        return [only]
    return [r[0] for r in conn.execute(
        "SELECT DISTINCT c.domain FROM hits h JOIN competitor_accounts c ON c.id=h.competitor_id "
        "WHERE h.dna_note_path IS NOT NULL AND h.dna_note_path!='' AND c.domain IS NOT NULL")]


def _is_done(dim: str) -> bool:
    fname = "选题判断维度.md" if dim == CRITERIA_DIM else f"{dim}打法路由表.md"
    return (VAULT_METHOD / fname).exists()


def run_batch(domain_only: str | None, force: bool) -> None:
    conn = connect()
    try:
        domains = _domains(conn, domain_only)
        if not domains:
            print("没有已拆 DNA 的领域,先跑 dna.py --batch。")
            return
        units = [(d, dim) for d in domains for dim in list(DIMS) + [CRITERIA_DIM]]
        todo = units if force else [(d, dim) for d, dim in units if not _is_done(dim)]
        if not todo:
            print("所有维度路由表/判断维度都在,无待跑(--force 可重跑)。看 --status。")
            return
        _log(f"reduce 开跑: 领域 {domains},待跑 {len(todo)} 个维度单元(sonnet)。撞限自动停、记断点。")
        for domain, dim in todo:
            try:
                if dim == CRITERIA_DIM:
                    run_criteria(conn, domain, dry_run=False)
                else:
                    run_dim(conn, dim, domain, dry_run=False)
            except SessionLimitError as e:
                _log(f"⏸ 撞订阅会话上限:[{domain}/{dim}] 未完(已完成的维度已落盘)。"
                     f"{e.reset_at:%H:%M} 重置后跑 `--batch` 自动续。")
                return
            except Exception as e:
                _log(f"⚠️ [{domain}/{dim}] 失败,跳过:{str(e)[:150]}")
                continue
        _log("✅ reduce 全部维度完成。范例库已填充,看 vault/范例/ 与 vault/方法论/。")
    finally:
        conn.close()


def status() -> None:
    conn = connect()
    try:
        domains = _domains(conn, None)
        print(f"已拆 DNA 的领域: {domains or '(无)'}")
        for dim in DIMS:
            done = "✓已跑" if _is_done(dim) else "⬜未跑"
            folder = VAULT_EXAMPLES / dim
            n_cards = len([p for p in folder.glob("*.md") if not p.name.startswith("_")]) if folder.exists() else 0
            print(f"  {dim}: {done}  范例 {n_cards} 张  (路由表 vault/方法论/{dim}打法路由表.md)")
        cdone = "✓已跑" if _is_done(CRITERIA_DIM) else "⬜未跑"
        print(f"  {CRITERIA_DIM}: {cdone}  (vault/方法论/选题判断维度.md)")
    finally:
        conn.close()


def main() -> None:
    p = argparse.ArgumentParser()
    g = p.add_mutually_exclusive_group(required=True)
    g.add_argument("--batch", action="store_true", help="跑/续:所有未跑维度")
    g.add_argument("--dim", choices=list(DIMS) + [CRITERIA_DIM], help="只跑一个维度")
    g.add_argument("--status", action="store_true", help="看进度")
    p.add_argument("--domain", help="限定领域(默认全领域各跑)")
    p.add_argument("--dry-run", action="store_true", help="配 --dim:只打印产出不落盘")
    p.add_argument("--force", action="store_true", help="配 --batch:已跑的维度也重跑")
    a = p.parse_args()
    if a.status:
        status()
    elif a.dim:
        conn = connect()
        try:
            dom = a.domain or (_domains(conn, None) or [None])[0]
            if not dom:
                sys.exit("没有已拆 DNA 的领域。")
            if a.dim == CRITERIA_DIM:
                run_criteria(conn, dom, dry_run=a.dry_run)
            else:
                run_dim(conn, a.dim, dom, dry_run=a.dry_run)
        except SessionLimitError as e:
            sys.exit(f"⏸ 撞会话上限,未完。{e.reset_at:%H:%M} 后再跑。")
        finally:
            conn.close()
    else:
        run_batch(a.domain, a.force)


if __name__ == "__main__":
    main()
