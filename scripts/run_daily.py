"""日常主链(挂 Windows 任务计划,每天一跑):采集增量 → 爆款判定 → 每日选题推飞书。

编排写死(章程:一个主脚本跑完日常,无决策点);任一步失败 → 飞书喊话 + 非零退出。
日志: logs/daily_YYYYMMDD.log
"""
import subprocess
import sys
from datetime import datetime
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
LOG = ROOT / "logs" / f"daily_{datetime.now():%Y%m%d}.log"
PY = sys.executable
DB = ROOT / "data" / "creation.db"
ASR_PY = ROOT / "tools" / "asr" / ".venv" / "Scripts" / "python.exe"   # 整理工跑在 asr 环境
ASR_PYW = ROOT / "tools" / "asr" / ".venv" / "Scripts" / "pythonw.exe"
PREP_WORKER = ROOT / "tools" / "asr" / "reverse_prep_worker.py"

STEPS = [
    ("采集增量", [PY, str(ROOT / "scripts/collect/crawl_competitors.py"), "--mode", "daily"]),
    ("爆款判定", [PY, str(ROOT / "scripts/analyze/judge_hits.py")]),
    ("每日选题", [PY, str(ROOT / "scripts/topics/daily_topics.py")]),
]


def shout(msg: str) -> None:
    """失败飞书喊话(喊话本身失败不掩盖原错误)。"""
    try:
        sys.path.insert(0, str(ROOT / "scripts" / "feishu"))
        import push
        push.send_text(msg)
    except Exception as e:
        print(f"飞书喊话也失败了: {e}")


def hidden_startupinfo():
    if sys.platform != "win32":
        return None
    si = subprocess.STARTUPINFO()
    si.dwFlags |= subprocess.STARTF_USESHOWWINDOW
    si.wShowWindow = subprocess.SW_HIDE
    return si


def hidden_creationflags(extra: int = 0) -> int:
    if sys.platform != "win32":
        return 0
    return extra | subprocess.CREATE_NO_WINDOW


def kick_reverse_prep(log) -> None:
    """解耦衔接:有新爆款(待备料)→ 后台踢起整理工(asr 环境:分组算分+转写+评论),不阻塞、不等待。

    判断"有新货"= 存在 reverse_status 待备料的 hit(judge_hits 新晋爆款默认 'none')。
    拆 DNA(LLM 路由 reverse_dna,烧额度)不在整理工里,仍手动。
    """
    import sqlite3
    try:
        conn = sqlite3.connect(DB)
        n = conn.execute("SELECT count(*) FROM hits WHERE reverse_status IN "
                         "('none','queued','failed')").fetchone()[0]
        conn.close()
    except Exception as e:
        log.write(f"查待备料数失败,跳过踢整理工: {e}\n")
        return
    if n == 0:
        log.write("无新爆款待备料,不踢整理工。\n")
        return
    if not ASR_PY.exists():
        log.write(f"asr 环境 python 不在({ASR_PY}),跳过自动备料;请手动跑整理工。\n")
        return
    bg_python = ASR_PYW if ASR_PYW.exists() else ASR_PY
    flags = hidden_creationflags(
        subprocess.CREATE_NEW_PROCESS_GROUP if sys.platform == "win32" else 0
    )
    subprocess.Popen([str(bg_python), str(PREP_WORKER)], cwd=ROOT,
                     stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                     creationflags=flags, startupinfo=hidden_startupinfo())
    log.write(f"有 {n} 条新爆款待备料 → 已后台踢起整理工(分组算分+转写+评论,日志 reverse_prep.log)。\n")


def main() -> None:
    LOG.parent.mkdir(exist_ok=True)
    with open(LOG, "a", encoding="utf-8") as log:
        for name, cmd in STEPS:
            log.write(f"\n===== {name} {datetime.now():%H:%M:%S} =====\n")
            log.flush()
            r = subprocess.run(cmd, stdout=log, stderr=subprocess.STDOUT,
                               cwd=ROOT, encoding="utf-8",
                               creationflags=hidden_creationflags(),
                               startupinfo=hidden_startupinfo())
            if r.returncode != 0:
                shout(f"⚠️ 日常主链在「{name}」失败(退出码 {r.returncode})。"
                      f"看日志: {LOG}")
                sys.exit(r.returncode)
        kick_reverse_prep(log)                          # 选题推完,顺手踢备料(解耦·异步)
        log.write(f"===== 全链完成 {datetime.now():%H:%M:%S} =====\n")


if __name__ == "__main__":
    main()
