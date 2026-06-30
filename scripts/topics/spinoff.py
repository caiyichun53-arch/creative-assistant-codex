"""衍生开题(题目优先备料通路·第二段):从一个已备料选题的研究/拆解里发现的**新方向**,
立成独立新选题、就这个方向从零重搜。

这是用户要的主路:二创某爆款 → 研究搜增量冒出信息缺口/争议 + 拆解给出可裂变选题 →
把这些"新方向"收出来挑一个 → 建 source_type='spinoff' 的新选题(挂 parent 爆款当出处)→
跑 prepare_topic(走从零重搜分支,不二创原爆款)。

方向来源(确定性抽取,零 LLM):
  - 父选题 research.md 的「信息缺口」「反向与争议」段(=搜增量发现的新方向)
  - 父选题来源爆款拆解笔记的「可裂变选题」段(已结构化的衍生方向)

用法:
  python scripts/topics/spinoff.py --from-topic 32              # 列出新方向(编号)
  python scripts/topics/spinoff.py --from-topic 32 --pick 3     # 选第3个→建衍生选题+备料
  python scripts/topics/spinoff.py --from-topic 32 --pick 3 --as "清理后的标题"  # 自定标题
  python scripts/topics/spinoff.py --from-topic 32 --pick 3 --no-prep            # 只建不备料
"""
import argparse
import re
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "scripts" / "collect"))
sys.stdout.reconfigure(encoding="utf-8", errors="replace")
from common import connect  # noqa: E402

PY = sys.executable

_BOLD = re.compile(r"\*\*(.+?)\*\*")
_NUM = re.compile(r"^\s*\d+[.、]\s*(.+)$")
# 标题里去掉"缺口1:""争议2、""方向3."这类前缀
_LEAD = re.compile(r"^(缺口|争议|方向|裂变|衍生)?\s*\d+\s*[：:、.]\s*")


def _section(text: str, prefix: str) -> str:
    """取 '## prefix...' 段正文(到下一个 ## 或结尾)。"""
    secs, cur, buf = {}, None, []
    for ln in text.splitlines():
        if ln.startswith("## "):
            if cur is not None:
                secs[cur] = "\n".join(buf)
            cur, buf = ln[3:].strip(), []
        elif cur is not None:
            buf.append(ln)
    if cur is not None:
        secs[cur] = "\n".join(buf)
    for head, body in secs.items():
        if head.startswith(prefix):
            return body
    return ""


def _clean(s: str) -> str:
    return _LEAD.sub("", s.strip().strip("*# ")).strip(" —-：:*#")


def _src_badge(src: str) -> str:
    """延展来源可读标签。可裂变选题=从评论真追问长(dna.py),即评论延展——信号最强、最值得重点关注。"""
    return "🔥评论延展(从评论真追问长)" if "可裂变" in src else src


def harvest(topic_id: int) -> list[dict]:
    """收集父选题的新方向。返回 [{src, title, detail}]，去重保序。"""
    conn = connect()
    t = conn.execute(
        "SELECT t.*, h.dna_note_path FROM topics t LEFT JOIN hits h ON h.id=t.source_hit_id "
        "WHERE t.id=?", (topic_id,)).fetchone()
    conn.close()
    if not t:
        sys.exit(f"选题 {topic_id} 不存在")

    out, seen = [], set()

    def _add(src, title, detail=""):
        title = _clean(title)
        key = re.sub(r"\s+", "", title)[:24]
        if len(title) < 4 or key in seen:
            return
        seen.add(key)
        out.append({"src": src, "title": title, "detail": detail.strip()})

    # 1) 研究里的信息缺口 / 反向与争议:加粗小标题=方向
    research = ROOT / "data" / "topics" / str(topic_id) / "research.md"
    if research.exists():
        rtext = research.read_text(encoding="utf-8")
        for sec_name, src in (("信息缺口", "研究·信息缺口"), ("反向与争议", "研究·争议")):
            body = _section(rtext, sec_name)
            for m in _BOLD.finditer(body):
                _add(src, m.group(1))

    # 2) 来源爆款拆解的可裂变选题:编号项(优先加粗引子当标题)
    if t["dna_note_path"]:
        note = ROOT / t["dna_note_path"]
        if note.exists():
            seg = _section(note.read_text(encoding="utf-8"), "可裂变选题")
            for ln in seg.splitlines():
                # 两种格式都吃:"1. **标题**——说明"(序号在外)和 "**1. 标题**"(序号在加粗内)
                mn = _NUM.match(ln.strip().strip("*").strip())
                if not mn:
                    continue
                item = mn.group(1).strip()
                mb = _BOLD.search(item)
                _add("拆解·可裂变选题", mb.group(1) if mb else item, item)
    return out


def list_directions(topic_id: int) -> None:
    conn = connect()
    pt = conn.execute("SELECT title, domain FROM topics WHERE id=?", (topic_id,)).fetchone()
    conn.close()
    dirs = harvest(topic_id)
    if not dirs:
        sys.exit(f"选题 {topic_id} 没抽到新方向(它有 research.md / 来源爆款拆解吗?先跑 prepare_topic)。")
    print(f"选题[{topic_id}]《{pt['title']}》(领域 {pt['domain']})衍生出的新方向:\n")
    for i, d in enumerate(dirs, 1):
        print(f"  {i}. [{_src_badge(d['src'])}] {d['title']}")
    print(f"\n挑一个: python scripts/topics/spinoff.py --from-topic {topic_id} --pick <编号>")


def insert_spinoff(parent_topic_id: int, direction: dict, as_title: str | None = None):
    """纯插入(无 print/无 sys.exit,供 CLI 与 listener 共用)。返回 (新id, 出处hit)。"""
    conn = connect()
    pt = conn.execute("SELECT domain, source_hit_id FROM topics WHERE id=?",
                      (parent_topic_id,)).fetchone()
    title = (as_title or direction["title"]).strip()
    with conn:
        cur = conn.execute(
            """INSERT INTO topics(title, angle, domain, source_type, source_hit_id,
                                  derive_source, derive_detail, status, selected_at)
               VALUES(?,?,?, 'spinoff', ?, ?, ?, 'selected', datetime('now','localtime'))""",
            (title, direction["title"], pt["domain"], pt["source_hit_id"],
             direction["src"], direction.get("detail", "")))
    tid = cur.lastrowid
    src_hit = pt["source_hit_id"]
    conn.close()
    return tid, src_hit


def create_spinoff(topic_id: int, pick: int, as_title: str | None) -> int:
    dirs = harvest(topic_id)
    if not (1 <= pick <= len(dirs)):
        sys.exit(f"--pick {pick} 越界,共 {len(dirs)} 个方向。先不带 --pick 看列表。")
    d = dirs[pick - 1]
    tid, src_hit = insert_spinoff(topic_id, d, as_title)
    print(f"✅ 衍生选题已建: [{tid}] {(as_title or d['title']).strip()}\n"
          f"   ← 衍生自选题[{topic_id}] · 方向来源 {_src_badge(d['src'])} · 出处爆款 hit {src_hit}")
    return tid


def main() -> None:
    p = argparse.ArgumentParser(description="从已备料选题的研究/拆解里发现的新方向衍生开题")
    p.add_argument("--from-topic", type=int, required=True, help="父选题 id(已备料·有research/拆解)")
    p.add_argument("--pick", type=int, help="选第几个方向(不带=只列表)")
    p.add_argument("--as", dest="as_title", help="自定衍生选题标题(默认用方向原文)")
    p.add_argument("--no-prep", action="store_true", help="只建选题、不自动备料")
    a = p.parse_args()

    if not a.pick:
        list_directions(a.from_topic)
        return
    tid = create_spinoff(a.from_topic, a.pick, a.as_title)
    if a.no_prep:
        print(f"下一步备料: python scripts/topics/prepare_topic.py {tid}")
        return
    print(f"--- 开始备料(prepare_topic {tid},从零重搜这个新方向)")
    r = subprocess.run([PY, str(ROOT / "scripts" / "topics" / "prepare_topic.py"), str(tid)], cwd=ROOT)
    sys.exit(r.returncode)


if __name__ == "__main__":
    main()
