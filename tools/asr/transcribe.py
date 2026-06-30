"""本地 ASR 批量转写工作器(在 tools/asr/.venv 跑)。固定技术路线,无决策点。

一条龙(每条爆款):下载视频 → ffmpeg 提音频 → SenseVoice 转写(本地,纯确定性)
  → 质检门 → 写 data/transcripts/hit_<id>.txt + 回写 hits → 删 mp4/wav(零残留)。
转写不接 LLM:生文本直接落库(同音错字/分段交给下游 opus 拆 DNA 时通读处理,
  不为清洗单独烧订阅会话额度。2026-06-14 砍掉 transcript_fix/haiku 节点)。
模型循环外加载一次,吃完整个队列(不一条一启)。

用法(必须用 venv 的 python):
  tools/asr/.venv/Scripts/python.exe tools/asr/transcribe.py --all-pending
  tools/asr/.venv/Scripts/python.exe tools/asr/transcribe.py --hit 5
"""
import argparse
import json
import re
import sqlite3
import subprocess
import sys
import tempfile
import time
from pathlib import Path

import requests
import yaml

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "scripts" / "collect"))
sys.path.insert(0, str(ROOT / "scripts" / "reverse"))
sys.stdout.reconfigure(encoding="utf-8", errors="replace")
from common import fetch_fresh_aweme, safe_title  # noqa: E402
from fetch_comments import store_hit_comments  # noqa: E402  备料合并:评论存储复用件

SETTINGS = yaml.safe_load((ROOT / "config" / "settings.yaml").read_text(encoding="utf-8"))
RCFG = SETTINGS["reverse_engine"]
MODELS_ROOT = Path(RCFG["models_root"])              # 公共模型库,本地离线加载
ASR_MODEL = str(MODELS_ROOT / RCFG["asr_model"])
VAD_MODEL = str(MODELS_ROOT / RCFG["vad_model"])
DB = ROOT / "data" / "creation.db"
OUT_DIR = ROOT / "data" / "transcripts"
NO_WINDOW = subprocess.CREATE_NO_WINDOW if sys.platform == "win32" else 0  # ffmpeg 提音频别闪控制台
UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/124.0 Safari/537.36")

EMOJI = re.compile(r"[\U0001F000-\U0001FAFF\U00002600-\U000027BF\U0001F300-\U0001F9FF]")


def strip_marks(text: str) -> str:
    return EMOJI.sub("", text).strip()


def download(url: str, dst: Path, retries: int = 3) -> None:
    """流式下载,带重试。play 端点会 302 跳 CDN 易截断(IncompleteRead)→ 重试兜底。"""
    last = None
    for attempt in range(1, retries + 1):
        try:
            with requests.get(url, headers={"User-Agent": UA,
                                            "Referer": "https://www.douyin.com/"},
                              timeout=120, stream=True) as r:
                r.raise_for_status()
                with open(dst, "wb") as f:
                    for chunk in r.iter_content(1 << 16):
                        f.write(chunk)
            if dst.stat().st_size > 0:
                return
            last = RuntimeError("下载文件为空")
        except Exception as e:                     # IncompleteRead/ChunkedEncoding/连接断
            last = e
        time.sleep(1.5 * attempt)
    raise RuntimeError(f"下载失败(重试{retries}次): {last}")


def extract_audio(ff: str, mp4: Path, wav: Path) -> None:
    subprocess.run([ff, "-y", "-i", str(mp4), "-vn", "-ac", "1", "-ar", "16000", str(wav)],
                   capture_output=True, check=True, creationflags=NO_WINDOW)


def shout(msg: str) -> None:
    try:
        sys.path.insert(0, str(ROOT / "scripts" / "feishu"))
        import push
        push.send_text(msg)
    except Exception:
        pass


def targets(conn, hit_id: int | None) -> list[sqlite3.Row]:
    if hit_id:
        return conn.execute("SELECT * FROM hits WHERE id=?", (hit_id,)).fetchall()
    return conn.execute(
        "SELECT * FROM hits WHERE reverse_status IN ('none','queued','extracting','failed') "
        "ORDER BY reverse_priority DESC, excess_ratio DESC").fetchall()


def main() -> None:
    import imageio_ffmpeg
    p = argparse.ArgumentParser()
    p.add_argument("--all-pending", action="store_true")
    p.add_argument("--hit", type=int)
    a = p.parse_args()

    conn = sqlite3.connect(DB)
    conn.row_factory = sqlite3.Row
    todo = targets(conn, a.hit)
    if not todo:
        print("没有待转写的爆款")
        return
    OUT_DIR.mkdir(parents=True, exist_ok=True)
    ff = imageio_ffmpeg.get_ffmpeg_exe()

    # 抖音下载链接带 sign= 时效签名,入库即过期 → 转写前用 detail 模式现取一批新鲜链接。
    print(f"现取 {len(todo)} 条新鲜下载链接+评论(MediaCrawler detail·备料合并)…")
    fresh, fresh_comments = fetch_fresh_aweme(
        [h["url"] for h in todo if h["url"]], with_comments=True)
    print(f"  取回 {len(fresh)} 条详情,{sum(len(v) for v in fresh_comments.values())} 条原始评论")

    print(f"加载 SenseVoice …({len(todo)} 条待转写)")
    from funasr import AutoModel
    from funasr.utils.postprocess_utils import rich_transcription_postprocess
    model = AutoModel(model=ASR_MODEL, vad_model=VAD_MODEL,
                      vad_kwargs={"max_single_segment_time": 30000}, disable_update=True)

    def asr(wav: Path) -> str:
        res = model.generate(input=str(wav), language="auto", use_itn=True,
                             batch_size_s=60, merge_vad=True, merge_length_s=15)
        return strip_marks("".join(rich_transcription_postprocess(r["text"]) for r in res))

    ok = fail = 0
    for h in todo:
        hid = h["id"]
        try:
            item = fresh.get(str(h["platform_item_id"]), {})
            mp3_url = item.get("music_download_url")
            video_url = item.get("video_download_url")
            if not (mp3_url or video_url):
                raise RuntimeError("无新鲜下载链接(detail 现取失败/视频已删)")
            min_chars = RCFG["min_transcript_chars"]
            text, src = "", ""
            with tempfile.TemporaryDirectory() as td:
                wav = Path(td) / "a.wav"
                # 优先音频:music 多为原声(含口播),直连 CDN·小·稳,绕开易截断的 play 端点。
                if mp3_url:
                    try:
                        mp3 = Path(td) / "a.mp3"
                        download(mp3_url, mp3)
                        extract_audio(ff, mp3, wav)
                        text, src = asr(wav), "音频"
                    except Exception as e:
                        print(f"    音频路线未成({e}),回退视频")
                # 字数过少疑似纯 BGM 无人声 → 回退整段视频
                if len(text) < min_chars and video_url:
                    mp4 = Path(td) / "v.mp4"
                    download(video_url, mp4)
                    extract_audio(ff, mp4, wav)
                    text, src = asr(wav), "视频"
            if len(text) < min_chars:
                raise RuntimeError(f"识别仅 {len(text)} 字,疑似纯BGM/识别失败")
            old = h["transcript_path"]
            if old and (ROOT / old).exists():
                (ROOT / old).unlink()          # 重跑:删旧名文件,避免改名后遗留
            stem = safe_title(h["title"], hid)
            tpath = OUT_DIR / f"{stem}.txt"
            if tpath.exists():                 # 标题撞名 → 加 id 区分
                tpath = OUT_DIR / f"{stem}_{hid}.txt"
            tpath.write_text(text, encoding="utf-8")
            with conn:
                conn.execute("UPDATE hits SET transcript_path=?, reverse_status='transcribed' "
                             "WHERE id=?", (str(tpath.relative_to(ROOT)), hid))
            ok += 1
            print(f"  ✓ hit {hid}: {len(text)}字[{src}] {h['title'][:24] if h['title'] else ''}")
        except Exception as e:
            with conn:
                conn.execute("UPDATE hits SET reverse_status='failed' WHERE id=?", (hid,))
            fail += 1
            print(f"  ✗ hit {hid}: {e}")
        # 备料合并:这条评论(同一趟 detail 爬已带回)过滤入库,与转写成败无关;幂等可重跑
        cmts = fresh_comments.get(str(h["platform_item_id"]), [])
        if cmts:
            try:
                kept = store_hit_comments(conn, h, cmts)   # top 默认 = comment_filter.TOP_COMMENTS(统一真值)
                print(f"    +评论 {len(cmts)}→{kept} 留存")
            except Exception as e:
                print(f"    评论存储失败(不影响转写): {e}")
    conn.close()
    msg = f"转写完成: 成功 {ok} / 失败 {fail}"
    print(msg)
    if fail:
        shout(f"⚠️ ASR 批量转写 {fail} 条失败(已标 failed,可重跑),成功 {ok}")


if __name__ == "__main__":
    main()
