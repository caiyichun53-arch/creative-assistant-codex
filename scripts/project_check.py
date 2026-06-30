"""Creation Assistant project self-check.

Read-only checks for migration/runtime gaps. Default checks stay local and fast.
Pass --collect-smoke-url to run a real Douyin detail fetch through MediaCrawler.

Usage:
  python scripts/project_check.py
  python scripts/project_check.py --collect-smoke-url https://v.douyin.com/xxxx/
"""
from __future__ import annotations

import argparse
import importlib.util
import shutil
import sqlite3
import subprocess
import sys
from pathlib import Path

import yaml

ROOT = Path(__file__).resolve().parents[1]
sys.stdout.reconfigure(encoding="utf-8", errors="replace")
COLLECT_DIR = ROOT / "scripts" / "collect"
if str(COLLECT_DIR) not in sys.path:
    sys.path.insert(0, str(COLLECT_DIR))


def _ok(msg: str) -> tuple[str, str]:
    return ("OK", msg)


def _warn(msg: str) -> tuple[str, str]:
    return ("WARN", msg)


def _fail(msg: str) -> tuple[str, str]:
    return ("FAIL", msg)


def _run_version(cmd: list[str], label: str, timeout: int = 20) -> tuple[str, str]:
    try:
        r = subprocess.run(cmd, capture_output=True, text=True,
                           encoding="utf-8", errors="replace", timeout=timeout)
    except Exception as e:
        return _fail(f"{label} cannot execute: {e}")
    text = (r.stdout or r.stderr).strip()
    if r.returncode == 0:
        return _ok(f"{label} executable: {text}")
    return _fail(f"{label} failed: {text[:300]}")


def check_files(settings: dict) -> list[tuple[str, str]]:
    out = []
    for rel in ["AGENTS.md", "BUILD_PLAN.md", "config/settings.yaml", ".codex/agents/写手.toml"]:
        path = ROOT / rel
        out.append(_ok(f"{rel} exists") if path.exists() else _fail(f"{rel} missing"))

    vault = ROOT / settings.get("paths", {}).get("vault", "vault")
    if not vault.exists():
        out.append(_fail(f"vault missing: {vault}"))
    else:
        required = [
            "人设/张芝士.md",
            "真人写作基石.md",
            "方法论/结构打法路由表.md",
            "方法论/钩子打法.md",
        ]
        missing = [p for p in required if not (vault / p).exists()]
        out.append(_ok(f"vault ready: {vault}") if not missing
                   else _warn("vault exists but missing: " + ", ".join(missing)))

    claude = ROOT / ".claude"
    if not claude.exists():
        out.append(_fail(".claude missing; current creation workflow uses Claude-side SOP and internal manuals"))
    else:
        required_claude = [
            "commands/创作.md",
            "agents/写手.md",
            "skills/对齐/SKILL.md",
            "skills/钩子/SKILL.md",
            "skills/钩子审核/SKILL.md",
            "skills/大纲/SKILL.md",
            "skills/成稿/SKILL.md",
            "skills/审稿/SKILL.md",
            "skills/优化诊断/SKILL.md",
            "skills/精修/SKILL.md",
            "skills/humanizer-zh/SKILL.md",
        ]
        missing_claude = [p for p in required_claude if not (claude / p).exists()]
        out.append(_ok(".claude creation manuals ready") if not missing_claude
                   else _fail(".claude missing manuals: " + ", ".join(missing_claude)))
    return out


def check_codex(settings: dict) -> list[tuple[str, str]]:
    out = []
    llm = settings.get("llm", {})
    if llm.get("provider") != "codex":
        out.append(_warn(f"llm.provider={llm.get('provider')}, not codex"))
        return out

    cmd = llm.get("codex", {}).get("command", "codex")
    resolved = shutil.which(cmd)
    if not resolved:
        out.append(_fail(f"Codex CLI not found: {cmd}"))
        return out
    out.append(_ok(f"Codex CLI located: {resolved}"))
    out.append(_run_version([cmd, "--version"], "Codex CLI", timeout=10))
    return out


def check_db(settings: dict) -> list[tuple[str, str]]:
    db = ROOT / settings.get("paths", {}).get("db", "data/creation.db")
    if not db.exists():
        return [_fail(f"database missing: {db}")]
    try:
        conn = sqlite3.connect(db)
        counts = {}
        for table in ["competitor_accounts", "competitor_videos", "hits", "topics", "drafts"]:
            counts[table] = conn.execute(f"SELECT count(*) FROM {table}").fetchone()[0]
        conn.close()
    except Exception as e:
        return [_fail(f"database unreadable: {e}")]
    return [_ok("database readable: " + ", ".join(f"{k}={v}" for k, v in counts.items()))]


def check_runtime() -> list[tuple[str, str]]:
    out = []
    asr_py = ROOT / "tools" / "asr" / ".venv" / "Scripts" / "python.exe"
    if asr_py.exists():
        out.append(_ok(f"ASR venv exists: {asr_py}"))
        out.append(_run_version([str(asr_py), "--version"], "ASR venv Python"))
    else:
        out.append(_fail(f"ASR venv missing: {asr_py}; reverse prep and ad-hoc transcription are unavailable"))

    out.append(_ok("lark-oapi SDK installed") if importlib.util.find_spec("lark_oapi")
               else _fail("lark-oapi SDK missing; Feishu listener unavailable"))
    return out


def check_collect_runtime() -> list[tuple[str, str]]:
    out = []
    mc_dir = ROOT / "vendor" / "MediaCrawler"
    if not (mc_dir / "main.py").exists():
        return [_fail(f"MediaCrawler missing or incomplete: {mc_dir}")]
    out.append(_ok(f"MediaCrawler ready: {mc_dir}"))

    try:
        from common import find_mediacrawler_python, find_uv
    except Exception as e:
        return [_fail(f"collect common import failed: {e}")]

    try:
        uv = find_uv()
        out.append(_ok(f"uv located: {uv}"))
        out.append(_run_version([uv, "--version"], "uv"))
    except Exception as e:
        out.append(_fail(f"uv unavailable: {e}"))

    mc_py = find_mediacrawler_python()
    if mc_py:
        out.append(_ok(f"MediaCrawler Python located: {mc_py}"))
        out.append(_run_version([mc_py, "--version"], "MediaCrawler Python"))
    else:
        out.append(_fail("no usable Python found for MediaCrawler"))

    mc_env = mc_dir / ".venv312" / "Scripts" / "python.exe"
    if mc_env.exists():
        out.append(_ok(f"MediaCrawler .venv312 exists: {mc_env}"))
        out.append(_run_version([str(mc_env), "--version"], "MediaCrawler .venv312 Python"))
    else:
        out.append(_warn(f"MediaCrawler .venv312 missing: {mc_env}; first uv run will rebuild or fail"))

    cache_dir = ROOT / "data" / "uv-cache"
    out.append(_ok(f"project uv cache exists: {cache_dir}") if cache_dir.exists()
               else _warn(f"project uv cache not created yet: {cache_dir}; first collection will create it"))
    return out


def check_collect_smoke(url: str) -> list[tuple[str, str]]:
    try:
        from common import fetch_fresh_aweme
        fresh = fetch_fresh_aweme([url], with_comments=False)
    except SystemExit as e:
        return [_fail(f"Douyin detail smoke test interrupted by lock/system exit: {e}")]
    except Exception as e:
        return [_fail(f"Douyin detail smoke test failed: {e}")]

    if not fresh:
        return [_fail("Douyin detail smoke test returned no video; short link or login state may be invalid")]

    aweme_id, item = next(iter(fresh.items()))
    has_media = bool(item.get("music_download_url") or item.get("video_download_url"))
    title = (item.get("title") or item.get("desc") or "")[:60]
    if has_media:
        return [_ok(f"Douyin detail smoke test passed: aweme_id={aweme_id}, title={title}")]
    return [_fail(f"Douyin detail returned no media download URL: aweme_id={aweme_id}, title={title}")]


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument(
        "--collect-smoke-url",
        help="Optional: run a real MediaCrawler Douyin detail check with this URL.",
    )
    args = parser.parse_args()

    settings_path = ROOT / "config" / "settings.yaml"
    settings = yaml.safe_load(settings_path.read_text(encoding="utf-8")) if settings_path.exists() else {}

    checks = []
    checks.extend(check_files(settings))
    checks.extend(check_codex(settings))
    checks.extend(check_db(settings))
    checks.extend(check_runtime())
    checks.extend(check_collect_runtime())
    if args.collect_smoke_url:
        checks.extend(check_collect_smoke(args.collect_smoke_url))

    failed = False
    for status, msg in checks:
        print(f"[{status}] {msg}")
        failed = failed or status == "FAIL"
    return 1 if failed else 0


if __name__ == "__main__":
    raise SystemExit(main())
