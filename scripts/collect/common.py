"""采集模块公共件:DB 连接、设置、sec_uid 解析、浏览器锁、现取新鲜链接。纯确定性,无 LLM。"""
import json
import os
import re
import shutil
import sqlite3
import subprocess
import sys
import urllib.request
from datetime import datetime
from pathlib import Path

import yaml

ROOT = Path(__file__).resolve().parents[2]
DB_PATH = ROOT / "data" / "creation.db"
SETTINGS = yaml.safe_load((ROOT / "config" / "settings.yaml").read_text(encoding="utf-8"))
MC_DIR = ROOT / "vendor" / "MediaCrawler"
MC_JSONL_DIR = MC_DIR / "data" / "douyin" / "jsonl"
RAW_ARCHIVE = ROOT / "data" / "raw" / "mediacrawler"
LOCK_FILE = MC_DIR / ".crawl.lock"

SEC_UID_RE = re.compile(r"MS4wLjAB[\w-]+")
UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/124.0 Safari/537.36")

# 抖音返回的下载链接带 sign= 时效签名,几小时即过期 → 不入库(入库=死链,转写时现取)。
EPHEMERAL_URL_KEYS = ("video_download_url", "music_download_url", "note_download_url")

# 定时任务以 pythonw(无控制台)为父进程,启动控制台子进程(uv/powershell)会被 Windows
# 新开控制台窗口抢焦点 → CREATE_NO_WINDOW 抑制。与无头/登录态无关。
NO_WINDOW = subprocess.CREATE_NO_WINDOW if sys.platform == "win32" else 0


def hidden_startupinfo():
    if sys.platform != "win32":
        return None
    si = subprocess.STARTUPINFO()
    si.dwFlags |= subprocess.STARTF_USESHOWWINDOW
    si.wShowWindow = subprocess.SW_HIDE
    return si


def find_uv() -> str:
    found = shutil.which("uv")
    if found:
        return found
    candidates = []
    userprofile = os.getenv("USERPROFILE")
    localappdata = os.getenv("LOCALAPPDATA")
    uv_exe = os.getenv("UV_EXE")
    if uv_exe:
        candidates.append(Path(uv_exe))
    if userprofile:
        candidates.append(Path(userprofile) / ".local" / "bin" / "uv.exe")
    if localappdata:
        candidates.append(Path(localappdata) / "Programs" / "uv" / "uv.exe")
    for path in candidates:
        if path.exists():
            return str(path)
    raise RuntimeError("uv.exe not found; install uv or add it to PATH")


def find_mediacrawler_python() -> str | None:
    candidates = []
    env_python = os.getenv("MEDIACRAWLER_PYTHON")
    if env_python:
        candidates.append(Path(env_python))
    candidates.extend([
        ROOT / "tools" / "python311" / "python.exe",
        Path(sys.executable),
    ])
    for path in candidates:
        if path.exists():
            return str(path)
    return None


def connect() -> sqlite3.Connection:
    conn = sqlite3.connect(DB_PATH)
    conn.execute("PRAGMA foreign_keys = ON")
    conn.row_factory = sqlite3.Row
    return conn


def parse_sec_uid(text: str) -> str | None:
    """从 sec_uid / 主页链接 / 短链 解析出 sec_uid。解析失败返回 None。"""
    text = text.strip()
    m = SEC_UID_RE.search(text)
    if m:
        return m.group(0)
    if "v.douyin.com" in text:
        url_m = re.search(r"https?://v\.douyin\.com/[\w-]+/?", text)
        if url_m:
            req = urllib.request.Request(url_m.group(0), headers={"User-Agent": UA})
            try:
                with urllib.request.urlopen(req, timeout=15) as resp:
                    final_url = resp.geturl()
                m = SEC_UID_RE.search(final_url)
                if m:
                    return m.group(0)
            except Exception as e:
                print(f"短链解析失败: {e}", file=sys.stderr)
    return None


def to_int(v) -> int | None:
    """MediaCrawler 计数字段是字符串且可能为 'None'。"""
    try:
        return int(v)
    except (TypeError, ValueError):
        return None


def strip_ephemeral_urls(item: dict) -> dict:
    """剔除时效下载链接后的浅拷贝(入库快照用;下载链接转写时现取)。"""
    return {k: v for k, v in item.items() if k not in EPHEMERAL_URL_KEYS}


_EMOJI = re.compile(r"[\U0001F000-\U0001FAFF\U00002600-\U000027BF\U0001F300-\U0001F9FF]")
_TAG = re.compile(r"#\S+")               # 抖音 #标签
_INVALID = re.compile(r'[\\/:*?"<>|]')   # Windows 文件名非法字符


def safe_title(title: str | None, fallback_id) -> str:
    """抖音标题 → 安全文件名(去 #标签/emoji/非法字符,折行转空格,截断 40 字)。
    转写文案、DNA 笔记共用,文件名口径一致。空则回退 hit_<id>。"""
    t = _TAG.sub("", title or "")
    t = _EMOJI.sub("", t)
    t = _INVALID.sub("", t)
    t = re.sub(r"\s+", " ", t).strip(" .,，。！!？?、·-—_")
    return t[:40].strip() or f"hit_{fallback_id}"


# ---- 浏览器互斥锁 + 残留清理(采集 / 现取链接共用一套 CDP 浏览器配置目录,必须互斥)----

def acquire_lock() -> None:
    if LOCK_FILE.exists():
        pid = LOCK_FILE.read_text().strip()
        sys.exit(f"已有采集/取链在跑(pid={pid},锁 {LOCK_FILE})。确认没在跑可删锁重试。")
    LOCK_FILE.write_text(str(os.getpid()))


def release_lock() -> None:
    LOCK_FILE.unlink(missing_ok=True)


def kill_leftover_browser() -> None:
    """清掉上次没死透的爬虫 Chrome(只杀用爬虫专用配置目录的,不碰用户日常浏览器)。"""
    subprocess.run(
        ["powershell", "-NoProfile", "-Command",
         "Get-CimInstance Win32_Process -Filter \"Name='chrome.exe'\" | "
         "Where-Object { $_.CommandLine -match 'cdp_dy_user_data_dir' } | "
         "ForEach-Object { Stop-Process -Id $_.ProcessId -Force -ErrorAction SilentlyContinue }"],
        capture_output=True, creationflags=NO_WINDOW, startupinfo=hidden_startupinfo())


def fetch_fresh_aweme(urls_or_ids: list[str], with_comments: bool = False):
    """现取一批视频的新鲜详情(含未过期下载链接)。固定路线:跑 MediaCrawler detail 模式。

    `with_comments=False`(默认):返回 {aweme_id: item_dict}(老行为不变)。
    `with_comments=True`:同一趟 detail 爬顺手带评论(--get_comment yes,每条上限由
        MC base_config CRAWLER_MAX_COMMENTS_COUNT_SINGLENOTES 控),返回
        ({aweme_id: item_dict}, {aweme_id: [评论行,...]})——备料合并:转写要的下载链接
        + 评论一趟拿齐,省第二趟爬。
    jsonl 输出读完即归档,不留给增量 ingest 误收。内部带浏览器互斥锁+残留清理;调用方不要再套锁。
    """
    if not urls_or_ids:
        return ({}, {}) if with_comments else {}
    before = set(MC_JSONL_DIR.glob("*.jsonl")) if MC_JSONL_DIR.exists() else set()
    acquire_lock()
    try:
        kill_leftover_browser()
        uv_cache_dir = ROOT / "data" / "uv-cache"
        uv_cache_dir.mkdir(parents=True, exist_ok=True)
        env = os.environ.copy()
        env["UV_CACHE_DIR"] = str(uv_cache_dir)
        env.setdefault("UV_PROJECT_ENVIRONMENT", str(MC_DIR / ".venv312"))

        cmd = [
            find_uv(), "run",
        ]
        mc_python = find_mediacrawler_python()
        if mc_python:
            cmd.extend(["--python", mc_python])
        cmd.extend([
            "main.py",
            "--platform", "dy", "--lt", "qrcode", "--type", "detail",
            "--specified_id", ",".join(urls_or_ids),
            "--save_data_option", "jsonl",
            "--get_comment", ("yes" if with_comments else "no"), "--get_sub_comment", "no",
            "--headless", "yes",
        ])
        subprocess.run(cmd, cwd=MC_DIR, env=env, check=True,
                       creationflags=NO_WINDOW, startupinfo=hidden_startupinfo())
    finally:
        release_lock()

    after = set(MC_JSONL_DIR.glob("*.jsonl")) if MC_JSONL_DIR.exists() else set()
    new_files = sorted(after - before)
    result: dict[str, dict] = {}
    comments: dict[str, list[dict]] = {}
    for f in new_files:
        for line in f.read_text(encoding="utf-8").splitlines():
            if not line.strip():
                continue
            item = json.loads(line)
            if item.get("comment_id"):                 # 评论行(视频行无 comment_id)
                comments.setdefault(str(item.get("aweme_id")), []).append(item)
            elif item.get("aweme_id"):                 # 视频详情行
                result[str(item["aweme_id"])] = item
    # 归档:detail 输出不能进增量 ingest(否则把现取的视频误当增量入库)
    if new_files:
        RAW_ARCHIVE.mkdir(parents=True, exist_ok=True)
        stamp = datetime.now().strftime("%Y%m%d_%H%M%S")
        for f in new_files:
            shutil.move(str(f), RAW_ARCHIVE / f"detail_{stamp}_{f.name}")
    return (result, comments) if with_comments else result
