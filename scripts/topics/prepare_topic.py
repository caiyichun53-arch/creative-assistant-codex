"""选中选题的备料流水线(确定性编排):拆DNA → 简版研究 → 组装材料包 → 飞书喊话。

由 listener 在收到「文案 N」后异步触发;也可手动跑。
材料包 = data/topics/<id>/brief.md(创作对话的唯一装载入口)。
用法: python scripts/topics/prepare_topic.py <topic_id>
"""
import subprocess
import sys
import argparse
from datetime import datetime
from pathlib import Path

import yaml

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "scripts" / "collect"))
sys.path.insert(0, str(ROOT / "scripts" / "feishu"))
sys.stdout.reconfigure(encoding="utf-8", errors="replace")
from common import connect  # noqa: E402
import push  # noqa: E402

PY = sys.executable

# 写手版 brief 要剔除的段(标题前缀匹配):
# - 来源口播文案/金句 = 逐字源文,留着写手会照搬复述(实测验证)
# - 洞察 = research 预写好的钩子/结构/立意,留着写手退化成渲染器、文风被锁死(实测验证)
# - Hook/结构/可裂变选题 = 对标拆解里预定好的钩子结构与裂变角度,同样抢戏
# 这些只留在 brief_full.md(规划用 + 下游复述查重用);写手版只给"生料 + 为什么火的分析"。
WRITER_DROP = ("Hook", "结构", "金句", "可裂变选题", "洞察", "来源口播文案")


def _strip_for_writer(text: str) -> str:
    """按 ## 段标题前缀剔除 WRITER_DROP 各段,产出写手版 brief。"""
    out, skip = [], False
    for ln in text.splitlines(keepends=True):
        if ln.startswith("## "):
            skip = ln[3:].strip().startswith(WRITER_DROP)
        elif ln.startswith("# "):
            skip = False  # 遇到 h1(如内嵌 DNA 笔记标题)复位
        if not skip:
            out.append(ln)
    return "".join(out)


def run_step(name: str, cmd: list[str]) -> None:
    print(f"--- {name} {datetime.now():%H:%M:%S}")
    r = subprocess.run(cmd, cwd=ROOT)
    if r.returncode != 0:
        push.send_text(f"⚠️ 备料流水线在「{name}」失败(选题流转中断),看 logs/listener.log")
        sys.exit(r.returncode)


def assemble(topic_id: int) -> Path:
    conn = connect()
    t = conn.execute(
        """SELECT t.*, h.id hit_id, h.title hit_title, h.url hit_url, h.like_count,
                  h.excess_ratio, h.share_comment_ratio, h.transcript_path, h.dna_note_path,
                  c.name comp_name
           FROM topics t LEFT JOIN hits h ON h.id=t.source_hit_id
           LEFT JOIN competitor_accounts c ON c.id=h.competitor_id
           WHERE t.id=?""", (topic_id,)).fetchone()
    acct = conn.execute("SELECT name, persona_path FROM accounts WHERE domain=? LIMIT 1",
                        (t["domain"],)).fetchone()
    conn.close()

    # 目标篇幅:从领域配置读(数据锚=对标爆款文案字数中位,非代码默认);账号覆盖以后再加
    dom_file = ROOT / "config" / "domains" / f"{t['domain']}.yaml"
    dom_cfg = yaml.safe_load(dom_file.read_text(encoding="utf-8")) if dom_file.exists() else {}
    tgt = (dom_cfg or {}).get("script_target_chars")
    rng = (dom_cfg or {}).get("script_chars_range")
    length_line = (f"目标 ~{tgt} 字" + (f"(区间 {rng[0]}–{rng[1]} 字)" if rng else "")
                   + " · 口播文案字数,按此写,别用通用默认") if tgt \
        else "(领域未配置目标篇幅,按对标爆款文案量级写)"

    out_dir = ROOT / "data" / "topics" / str(topic_id)
    out_dir.mkdir(parents=True, exist_ok=True)   # 正常 research 先建;独立调 assemble 时兜底
    research = out_dir / "research.md"

    # 来源爆款三段只在「二创」(source_type='hit')时给。
    # 衍生(spinoff):源自某爆款的研究新方向,但这条是新选题、从零重搜——只标出处,不塞来源爆款原文。
    # 原创(original):从题目直接开,无来源。
    if t["source_type"] == "hit" and t["hit_id"]:
        transcript = (ROOT / t["transcript_path"]).read_text(encoding="utf-8") \
            if t["transcript_path"] else "(无)"
        dna = (ROOT / t["dna_note_path"]).read_text(encoding="utf-8") \
            if t["dna_note_path"] else "(无)"
        hit_block = f"""
## 来源爆款(二创参考:借选题/结构/钩子层套路,不借文笔)
{t['hit_title']} · {t['comp_name']} · {t['like_count']}赞 · 超额{t['excess_ratio']}x · [原片]({t['hit_url']})

## 对标爆款拆解
{dna}

## 来源口播文案(竞品讲过的,成稿必须避开复述)
{transcript}
"""
    elif t["source_type"] == "spinoff":
        origin = f"衍生自爆款《{t['hit_title']}》" if t["hit_title"] else "衍生方向"
        src = t["derive_source"] or ""
        via = "🔥评论延展(从评论真追问长)" if "可裂变" in src else (src or "未记录")
        detail = (t["derive_detail"] or "").strip()
        hit_block = (f"\n> {origin}——这是从该爆款研究里发现的**新方向**,已就这个方向从零重搜,"
                     f"是独立新选题(不二创原爆款)。\n> 延展来源:{via}"
                     + (f"\n> 来源原文:{detail}" if detail else "") + "\n")
    else:
        hit_block = "\n> 原创开题:无来源爆款,从题目直接研究创作(不二创)。\n"

    full = f"""# 材料包 · 选题[{topic_id}] {t['title']}

> 完整版(brief_full.md):规划 + 下游复述查重用。创作装载的是写手版 brief.md(已剔除来源原文/逐字金句/预写洞察以防复述与抢戏)。
> 账号:{acct['name']} | 人设:{acct['persona_path']} | 生成于 {datetime.now():%Y-%m-%d %H:%M}

## 选题
{t['title']}(领域:{t['domain']})

## 目标篇幅
{length_line}
{hit_block}
## 研究包(材料+洞察)
{research.read_text(encoding='utf-8') if research.exists() else '(无)'}
"""
    brief_full = out_dir / "brief_full.md"
    brief_full.write_text(full, encoding="utf-8")

    # 写手版:创作对话唯一装载入口。剔除会导致复述/抢戏的段。
    writer = _strip_for_writer(full).replace(
        "# 材料包 · 选题",
        "# 材料包(写手版·只给生料,钩子结构自己定) · 选题", 1)
    brief = out_dir / "brief.md"
    brief.write_text(writer, encoding="utf-8")
    return brief


def main(topic_id: int) -> None:
    conn = connect()
    t = conn.execute("SELECT t.source_type, t.source_hit_id, h.dna_note_path FROM topics t "
                     "LEFT JOIN hits h ON h.id=t.source_hit_id WHERE t.id=?",
                     (topic_id,)).fetchone()
    conn.close()
    # 只有二创(source_type='hit')才拆来源爆款 DNA;原创/衍生(original/spinoff)即便挂了 parent 爆款
    # 也不二创它,走从零重搜,不拆 DNA。
    if t["source_type"] == "hit" and t["source_hit_id"] and not t["dna_note_path"]:
        run_step("逆向拆DNA", [PY, str(ROOT / "scripts/reverse/dna.py"), "--hit", str(t["source_hit_id"])])
    run_step("简版研究", [PY, str(ROOT / "scripts/research/research.py"), str(topic_id)])
    brief = assemble(topic_id)
    push.send_text(f"📦 选题[{topic_id}] 材料包就绪: {brief.relative_to(ROOT)}\n"
                   f"回电脑上在 Claude Code 里输入: /创作 {topic_id}")
    print(f"材料包: {brief}")


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="为选题生成 brief_full.md 和写手版 brief.md 材料包")
    parser.add_argument("topic_id", type=int, help="选题 id")
    args = parser.parse_args()
    main(args.topic_id)
