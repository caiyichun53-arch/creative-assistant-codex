"""逆向备料整理工(asr venv · 后台 · 由 run_daily 在"有新爆款"时踢起;也可手动跑)。

确定性重活、无 LLM,顺序两步:
  ① 爆款分组 + 算排队分(cluster_hits:本地 embedding 聚同题簇 + reverse_priority)
  ② 备料 = 转写 + 评论(transcribe --all-pending:一趟 detail 爬拿下载链接+评论)
解耦缘由:这两步都要 asr venv 的本地模型,跟主环境的每日流程不同环境 → 单独一个整理工,
  由每日流程"踢一脚"自动衔接,互不阻塞。

★ 拆 DNA(LLM 路由 reverse_dna·烧额度)不在此 —— 仍由你空闲双击 `启动逆向拆DNA.bat` 手动跑。

日志: logs/reverse_prep.log
手动跑: tools/asr/.venv/Scripts/python.exe tools/asr/reverse_prep_worker.py
"""
import subprocess
import sys
from datetime import datetime
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
LOG = ROOT / "logs" / "reverse_prep.log"
PY = sys.executable          # 本脚本就该用 asr venv python 跑 → 子步骤复用同一解释器
# 整理工被 DETACHED 起、自身无控制台,起 python 子步骤时 Windows 会默认新建可见控制台 → 抑制
NO_WINDOW = subprocess.CREATE_NO_WINDOW if sys.platform == "win32" else 0
PYW = Path(PY).with_name("pythonw.exe")
if sys.platform == "win32" and PYW.exists():
    PY = str(PYW)


def hidden_startupinfo():
    if sys.platform != "win32":
        return None
    si = subprocess.STARTUPINFO()
    si.dwFlags |= subprocess.STARTF_USESHOWWINDOW
    si.wShowWindow = subprocess.SW_HIDE
    return si


def log(msg: str) -> None:
    line = f"{datetime.now():%m-%d %H:%M:%S} {msg}"
    print(line)
    try:
        LOG.parent.mkdir(parents=True, exist_ok=True)
        with LOG.open("a", encoding="utf-8") as f:
            f.write(line + "\n")
    except Exception:
        pass


def step(name: str, cmd: list[str]) -> None:
    log(f"--- {name} 开始")
    r = subprocess.run(cmd, cwd=ROOT, creationflags=NO_WINDOW,
                       startupinfo=hidden_startupinfo())
    log(f"--- {name} {'完成' if r.returncode == 0 else f'失败(rc={r.returncode}),继续下一步'}")


def main() -> None:
    log("整理工启动:分组算分 → 备料(转写+评论)")
    # 先聚类算分(给新爆款定簇+排队分),再备料(转写按 priority 排序);任一步失败不拖垮另一步
    step("爆款分组+算排队分", [PY, str(ROOT / "scripts" / "analyze" / "cluster_hits.py")])
    step("备料(转写+评论)", [PY, str(ROOT / "tools" / "asr" / "transcribe.py"), "--all-pending"])
    log("整理工结束。拆 DNA 请空闲双击 启动逆向拆DNA.bat(LLM·手动·不自动跑)。")


if __name__ == "__main__":
    main()
