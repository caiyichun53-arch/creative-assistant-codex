"""飞书指令监听端(常驻):接收你在飞书里的回复,驱动选题流转。

指令(私聊机器人):
  选 12          → 选中选题12:状态selected,回发下一步指引(本地 ASR 转写来源爆款)
  弃 12          → 沉底:状态expired
  文案 12 <正文>  → 手动兜底:直接登记选题12来源爆款的口播文案(通常无需,本地 ASR 已自动转写)
                   → 自动跑 逆向DNA+简版研究+组装材料包(耗时几分钟,完成后飞书喊你)
  提炼           → 后台启动真人写作提炼(map,断点续跑;撞限/完成都飞书喊你)
  提炼进度        → 看真人写作提炼进度
其余消息忽略(不接 LLM,纯命令解析——封闭指令集,LLM 不握轨道)。

运行: python scripts/feishu/listener.py   (开机自启任务 CreationAssistant_Listener)
日志: logs/listener.log
"""
import json
import re
import shutil
import subprocess
import sys
import time
from datetime import datetime
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "scripts" / "collect"))
sys.path.insert(0, str(ROOT / "scripts" / "feishu"))
sys.path.insert(0, str(ROOT / "scripts" / "topics"))
sys.stdout.reconfigure(encoding="utf-8", errors="replace")
from common import connect  # noqa: E402
import push  # noqa: E402
from spinoff import harvest, insert_spinoff  # noqa: E402

LOG = ROOT / "logs" / "listener.log"
TRANSCRIPT_DIR = ROOT / "data" / "transcripts"
PY = sys.executable
HUMANIZE = ROOT / "scripts" / "humanize" / "extract.py"
HUMANIZE_LOCK = ROOT / "data" / "humanize" / ".map.running"
HUMANIZE_LOG = ROOT / "logs" / "humanize.log"
NO_WINDOW = subprocess.CREATE_NO_WINDOW if sys.platform == "win32" else 0

CMD_SELECT = re.compile(r"^(?:选\s*)?(\d+)\s*$")   # "选 5" 或裸 "5" 都算选
CMD_DROP = re.compile(r"^弃\s*(\d+)\s*$")
CMD_SCRIPT = re.compile(r"^文案\s*(\d+)\s*[::\s]([\s\S]+)$")
CMD_HZ_STATUS = re.compile(r"^提炼进度\s*$")
CMD_HUMANIZE = re.compile(r"^(?:提炼|真人写作)\s*$")
CMD_SPINOFF_LIST = re.compile(r"^衍生\s*(\d+)\s*$")            # "衍生 32" = 列该选题的新方向
CMD_SPINOFF_PICK = re.compile(r"^衍生\s*(\d+)\s*[.\-、:：]\s*(\d+)\s*$")  # "衍生 32.3" = 选第3个
HELP = ("没看懂。用法:\n选 5(或直接发 5)= 选中选题5\n弃 5 = 沉底\n"
        "文案 5 <粘贴全文> = 登记口播文案\n衍生 5 = 看选题5研究出的新方向\n"
        "衍生 5.2 = 选第2个新方向立成衍生选题并备料\n提炼 = 后台跑真人写作提炼\n提炼进度 = 看进度")


def log(msg: str) -> None:
    with open(LOG, "a", encoding="utf-8") as f:
        f.write(f"{datetime.now():%m-%d %H:%M:%S} {msg}\n")


def topic_with_hit(conn, tid: int):
    return conn.execute(
        """SELECT t.*, h.id AS hit_id, h.url AS hit_url, h.title AS hit_title,
                  h.transcript_path AS hit_transcript
           FROM topics t LEFT JOIN hits h ON h.id = t.source_hit_id
           WHERE t.id=?""", (tid,)).fetchone()


def on_select(tid: int) -> str:
    conn = connect()
    row = topic_with_hit(conn, tid)
    if not row:
        conn.close()
        return f"没有选题 [{tid}]"
    with conn:
        conn.execute("UPDATE topics SET status='selected', "
                     "selected_at=datetime('now','localtime') WHERE id=?", (tid,))
    has_hit = row["hit_id"] is not None
    ready = bool(row["hit_transcript"])     # 来源爆款已备料(入库时转写,见 reverse-prep-vs-analysis)
    conn.close()
    if has_hit and ready:
        # 备料已就绪 → 直接后台跑 prepare_topic(拆DNA→研究→材料包),完成由流水线自己喊话
        subprocess.Popen(
            [PY, str(ROOT / "scripts" / "topics" / "prepare_topic.py"), str(tid)],
            cwd=ROOT, stdout=open(LOG, "a", encoding="utf-8"), stderr=subprocess.STDOUT)
        return (f"✅ 已选中 [{tid}] {row['title']}\n\n"
                f"来源爆款已备料,后台开跑:拆DNA → 简版研究 → 材料包(约几分钟),完成喊你。")
    if has_hit:
        return (f"✅ 已选中 [{tid}] {row['title']}\n\n"
                f"⚠️ 来源爆款还没备料(转写未完成),材料包暂不能自动跑。\n"
                f"原片: {row['hit_url']}\n"
                f"等备料跑到这条后重发『选 {tid}』,或人工兜底: 文案 {tid} <粘贴全文>。")
    return (f"✅ 已选中 [{tid}] {row['title']}\n(此选题无来源爆款,直接进研究/创作。)")


def on_drop(tid: int) -> str:
    conn = connect()
    with conn:
        cur = conn.execute("UPDATE topics SET status='expired' WHERE id=?", (tid,))
    conn.close()
    return f"🗑 已沉底 [{tid}]" if cur.rowcount else f"没有选题 [{tid}]"


def on_script(tid: int, text: str) -> str:
    conn = connect()
    row = topic_with_hit(conn, tid)
    if not row or not row["hit_id"]:
        conn.close()
        return f"选题 [{tid}] 不存在或没有来源爆款"
    TRANSCRIPT_DIR.mkdir(parents=True, exist_ok=True)
    tpath = TRANSCRIPT_DIR / f"hit_{row['hit_id']}.txt"
    tpath.write_text(text.strip(), encoding="utf-8")
    with conn:
        # 手填文案=终态(置 transcribed):否则 transcribe --all-pending 认 'extracting' 会重转覆盖手填稿
        conn.execute("UPDATE hits SET transcript_path=?, reverse_status='transcribed' "
                     "WHERE id=?", (str(tpath.relative_to(ROOT)), row["hit_id"]))
    conn.close()
    # 异步跑流水线(逆向DNA→研究→材料包),完成由流水线自己喊话
    subprocess.Popen(
        [PY, str(ROOT / "scripts" / "topics" / "prepare_topic.py"), str(tid)],
        cwd=ROOT, stdout=open(LOG, "a", encoding="utf-8"), stderr=subprocess.STDOUT)
    return (f"📝 文案已收({len(text.strip())}字)。后台开跑:拆DNA → 简版研究 → 材料包,"
            f"完成后喊你(约几分钟)。")


def on_spinoff_list(tid: int) -> str:
    conn = connect()
    pt = conn.execute("SELECT title FROM topics WHERE id=?", (tid,)).fetchone()
    conn.close()
    if not pt:
        return f"没有选题 [{tid}]"
    dirs = harvest(tid)
    if not dirs:
        return (f"选题 [{tid}] 没抽到新方向。\n"
                f"(要先备过料、有 research.md / 来源爆款拆解才有新方向可衍生。)")
    lines = [f"🌱 选题[{tid}]《{pt['title']}》衍生出的新方向:"]
    for i, d in enumerate(dirs, 1):
        lines.append(f"{i}. {d['title']}　—{d['src']}")
    lines.append(f"\n挑一个回:『衍生 {tid}.<编号>』→ 立成新选题并从零重搜。")
    return "\n".join(lines)


def on_spinoff_pick(tid: int, k: int) -> str:
    dirs = harvest(tid)
    if not dirs:
        return f"选题 [{tid}] 没抽到新方向(先备料)。"
    if not (1 <= k <= len(dirs)):
        return f"编号 {k} 越界,选题 [{tid}] 共 {len(dirs)} 个方向。发『衍生 {tid}』看列表。"
    d = dirs[k - 1]
    new_tid, src_hit = insert_spinoff(tid, d)
    # 异步备料(从零重搜这个新方向),完成由流水线自己喊话
    subprocess.Popen(
        [PY, str(ROOT / "scripts" / "topics" / "prepare_topic.py"), str(new_tid)],
        cwd=ROOT, stdout=open(LOG, "a", encoding="utf-8"), stderr=subprocess.STDOUT)
    return (f"✅ 衍生选题已建 [{new_tid}] {d['title']}\n"
            f"　← 衍生自选题[{tid}]({d['src']})\n\n"
            f"后台开跑:就这个新方向从零重搜 → 材料包(约几分钟),完成喊你。")


def on_humanize_start() -> str:
    if HUMANIZE_LOCK.exists() and time.time() - HUMANIZE_LOCK.stat().st_mtime < 900:
        return "真人写作提炼已经在跑了。发『提炼进度』看进度。"
    subprocess.Popen([PY, str(HUMANIZE), "--map"], cwd=ROOT,
                     stdout=open(HUMANIZE_LOG, "a", encoding="utf-8"),
                     stderr=subprocess.STDOUT, creationflags=NO_WINDOW)
    return ("🚀 真人写作提炼已后台开跑,一块一块消化。\n"
            "撞会话上限会自动停并喊你;全部完成也会喊你。随时发『提炼进度』看进度。")


def on_humanize_status() -> str:
    r = subprocess.run([PY, str(HUMANIZE), "--status"], cwd=ROOT, capture_output=True,
                       text=True, encoding="utf-8", errors="replace", creationflags=NO_WINDOW)
    return (r.stdout or r.stderr or "").strip() or "拿不到进度"


def handle(text: str) -> str | None:
    text = text.strip()
    if m := CMD_SELECT.match(text):
        return on_select(int(m.group(1)))
    if m := CMD_DROP.match(text):
        return on_drop(int(m.group(1)))
    if m := CMD_SCRIPT.match(text):
        return on_script(int(m.group(1)), m.group(2))
    if m := CMD_SPINOFF_PICK.match(text):          # "衍生 32.3" 先于 "衍生 32"(带点的先匹配)
        return on_spinoff_pick(int(m.group(1)), int(m.group(2)))
    if m := CMD_SPINOFF_LIST.match(text):
        return on_spinoff_list(int(m.group(1)))
    if CMD_HZ_STATUS.match(text):
        return on_humanize_status()
    if CMD_HUMANIZE.match(text):
        return on_humanize_start()
    if len(text) <= 20:
        return HELP  # 短消息大概率是想下指令没下对,给提示;长文本忽略
    return None


def main() -> None:
    lark = shutil.which("lark-cli")
    log("listener 启动")
    proc = subprocess.Popen(
        [lark, "event", "consume", "im.message.receive_v1", "--as", "bot", "--quiet"],
        stdin=subprocess.PIPE,  # 无界 consume 在 stdin EOF 时退出,挂着不写保持常驻
        stdout=subprocess.PIPE, stderr=subprocess.DEVNULL, text=True, encoding="utf-8")
    for line in proc.stdout:
        try:
            evt = json.loads(line)
            if evt.get("message_type") != "text":
                continue
            content = evt.get("content", "")
            # lark-cli 已把 content 解成纯文本;若是 JSON 字符串再剥一层
            if content.startswith("{"):
                content = json.loads(content).get("text", content)
            log(f"收到: {content[:80]}")
            reply = handle(content)
            if reply:
                push.send_text(reply)
                log(f"回复: {reply[:60]}")
        except Exception as e:
            log(f"处理出错: {e}")


if __name__ == "__main__":
    main()
