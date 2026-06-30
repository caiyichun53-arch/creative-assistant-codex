"""逆向拆 DNA(LLM 生成节点):口播文案 → 爆款拆解笔记(data/爆款拆解/,中间燃料)。

输入:hits 行(须已有 transcript_path)。输出:拆解笔记 md + 回写 hits.dna_note_path。
拆解框架:选题/Hook/结构/张力/可裂变选题(**不提金句**——爆款金句多半也是 AI 写的,
提了就有人拿去当文风料=AI味污染源,源头就不提)。

逐条拆(单篇=map 单元;reduce 找共性在另一脚本)。撞订阅会话上限 → 干净中止、记日志,
重置后跑同命令从断点续(已拆的靠 dna_note_path 幂等跳过,不重拆)。

用法:
  python scripts/reverse/dna.py --hit <id>   # 拆单条(临时/插队逆向)
  python scripts/reverse/dna.py --batch       # 跑/续:拆所有"已转写但没拆DNA"的爆款
  python scripts/reverse/dna.py --status       # 看进度
"""
import argparse
import sys
from datetime import date, datetime
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "scripts" / "collect"))
sys.path.insert(0, str(ROOT / "scripts" / "llm"))
sys.path.insert(0, str(ROOT / "scripts" / "reverse"))   # comment_filter 同目录,显式上路径保稳
sys.stdout.reconfigure(encoding="utf-8", errors="replace")
from common import connect, safe_title  # noqa: E402
from call import call_llm, load_prompt, SessionLimitError  # noqa: E402
from comment_filter import TOP_COMMENTS  # noqa: E402  评论数唯一真值

VAULT_DNA = ROOT / "data" / "爆款拆解"  # 中间燃料(与 data/transcripts 并列),非成品仓
LOG_FILE = ROOT / "logs" / "dna_batch.log"

PROMPT = load_prompt("拆解")  # 提示词搬进 .claude/skills/拆解/SKILL.md(唯一真相)


def _log(msg: str) -> None:
    """进度落本地日志(不推飞书):回来看 logs/dna_batch.log 即是记录。"""
    line = f"{datetime.now():%m-%d %H:%M:%S} {msg}"
    print(line)
    try:
        LOG_FILE.parent.mkdir(parents=True, exist_ok=True)
        with LOG_FILE.open("a", encoding="utf-8") as fh:
            fh.write(line + "\n")
    except Exception:
        pass


def _load_comments(conn, hit_id: int, top_n: int = TOP_COMMENTS) -> str:
    """取该爆款点赞最高的 top_n 条评论(hit_comments 入库前已过广告/灌水过滤),喂给拆解当实证。"""
    rows = conn.execute(
        "SELECT content, like_count FROM hit_comments WHERE hit_id=? "
        "ORDER BY like_count DESC LIMIT ?", (hit_id, top_n)).fetchall()
    if not rows:
        return "(无评论数据——共鸣/可裂变选题这两节据口播文案审慎推断,别硬编)"
    return "\n".join(f"[{r['like_count']}赞] {(r['content'] or '').strip()[:80]}" for r in rows)


def analyze_one(conn, h) -> str:
    """拆单条爆款 → 写拆解笔记 + 回写 hits。须已有 transcript_path。

    撞会话上限时 call_llm 抛 SessionLimitError,本函数不吞,交给调用方熔断。
    """
    if not h["transcript_path"]:
        raise RuntimeError(f"hit {h['id']} 还没有口播文案,跳过")
    transcript = (ROOT / h["transcript_path"]).read_text(encoding="utf-8")
    comments = _load_comments(conn, h["id"])

    body = call_llm("reverse_dna", PROMPT.format(
        title=h["title"], like_count=h["like_count"], comment_count=h["comment_count"],
        share_count=h["share_count"], excess=h["excess_ratio"],
        sc_ratio=h["share_comment_ratio"], transcript=transcript, comments=comments))

    comp = conn.execute("SELECT name, domain FROM competitor_accounts WHERE id=?",
                        (h["competitor_id"],)).fetchone()
    note = VAULT_DNA / f"{safe_title(h['title'], h['id'])}.md"
    note.parent.mkdir(parents=True, exist_ok=True)
    note.write_text(f"""---
type: 爆款拆解
domain: {comp['domain']}
hit_id: {h['id']}
title: "{h['title']}"
competitor: {comp['name']}
excess_ratio: {h['excess_ratio']}
tags: []
created: {date.today()}
---

# {h['title']}

## 数据
点赞 {h['like_count']} | 评论 {h['comment_count']} | 转发 {h['share_count']} | 超额 {h['excess_ratio']}x | 转评比 {h['share_comment_ratio']}

## 口播文案(本地 SenseVoice ASR 提取)
见 `{h['transcript_path']}`

{body}

## 晋升记录
(待:哪些部分提炼成了范例)
""", encoding="utf-8")

    with conn:
        conn.execute("UPDATE hits SET dna_note_path=?, reverse_status='done' WHERE id=?",
                     (str(note.relative_to(ROOT)), h["id"]))
    return str(note.relative_to(ROOT))


_ELIGIBLE = ("transcript_path IS NOT NULL AND transcript_path!='' "
             "AND (cluster_id IS NULL OR cluster_id = id)")   # 跳过洗稿/二创非代表(同簇只拆代表)


def _pending(conn):
    """已转写、是代表作/独一份、还没拆 DNA 的爆款(幂等续跑的依据)。

    排序:reverse_priority DESC(强度×新意,cluster_hits 算)→ 未算则跌穿到 excess_ratio 兜底。
    """
    return conn.execute(
        f"SELECT * FROM hits WHERE {_ELIGIBLE} "
        "AND (dna_note_path IS NULL OR dna_note_path='') "
        "ORDER BY reverse_priority DESC, excess_ratio DESC"
    ).fetchall()


def run_one(hit_id: int) -> None:
    conn = connect()
    h = conn.execute("SELECT * FROM hits WHERE id=?", (hit_id,)).fetchone()
    if not h:
        sys.exit(f"hit {hit_id} 不存在")
    try:
        path = analyze_one(conn, h)
    except SessionLimitError as e:
        sys.exit(f"⏸ 撞会话上限,未拆。{e.reset_at:%H:%M} 后再跑。")
    finally:
        conn.close()
    print(f"DNA笔记: {path}")


def run_batch() -> None:
    conn = connect()
    total = conn.execute(f"SELECT count(*) FROM hits WHERE {_ELIGIBLE}").fetchone()[0]
    pending = _pending(conn)
    done_at_start = total - len(pending)
    if not pending:
        conn.close()
        print(f"DNA 全部完成({total} 条已拆)。下一步:跑 reduce 归并共性。")
        return
    _log(f"开拆: 共 {total} 条已转写爆款,待拆 {len(pending)} 条(sonnet)。撞限自动停、记断点。")
    try:
        for h in pending:
            try:
                path = analyze_one(conn, h)
            except SessionLimitError as e:
                done = total - len(_pending(conn))
                gained = done - done_at_start
                _log(f"⏸ 撞订阅会话上限:本轮拆 {gained} 条,累计 {done}/{total}(已落盘·永不重拆),"
                     f"还剩 {total - done} 条。{e.reset_at:%H:%M} 重置后跑 `--batch` 自动续。")
                return
            except Exception as e:  # 单条坏掉不拖垮整批
                _log(f"⚠️ hit {h['id']} 拆解失败,跳过:{str(e)[:120]}")
                continue
            done = total - len(_pending(conn))
            _log(f"✓ hit {h['id']}（{h['title'][:18]}）→ {Path(path).name}  累计 {done}/{total}")
    finally:
        conn.close()
    _log(f"✅ DNA 批量全部完成({total} 条)。下一步:跑 reduce 归并共性 → 领域层范例。")


def status() -> None:
    conn = connect()
    total = conn.execute(f"SELECT count(*) FROM hits WHERE {_ELIGIBLE}").fetchone()[0]
    pend = len(_pending(conn))
    conn.close()
    print(f"待拆爆款(代表作+独一份,已转写): {total} 条\nDNA 进度: {total - pend}/{total} 已拆")
    print(f"剩 {pend} 条,跑 --batch 续。" if pend else "全拆完,可跑 reduce 归并共性。")


def main() -> None:
    p = argparse.ArgumentParser()
    g = p.add_mutually_exclusive_group(required=True)
    g.add_argument("--hit", type=int, help="拆单条(临时/插队逆向)")
    g.add_argument("--batch", action="store_true", help="跑/续:拆所有已转写未拆DNA的爆款")
    g.add_argument("--status", action="store_true", help="看进度")
    a = p.parse_args()
    if a.hit:
        run_one(a.hit)
    elif a.batch:
        run_batch()
    else:
        status()


if __name__ == "__main__":
    main()
