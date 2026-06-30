"""LLM 调用件(固定函数):per-node 路由,经 Claude Code 无头跑订阅。

路由表在 config/settings.yaml llm.routes;节点名→模型。
LLM 只在这里被调用,prompt 由调用方写死成模板——LLM 不握轨道。
cwd 指到系统临时目录,避免把本工程 CLAUDE.md 注进每次生成调用。

安全网(防炸窗/死循环,2026-06-14):
- 撞订阅会话上限(session limit)→ 抛 SessionLimitError(带重置时间),并把冷却截止
  写进 data/llm_state.json。
- 之后任何 call_llm 在冷却期内**直接 fail-fast,不再 spawn claude、不重读 prompt**
  → 重置前的重跑不会反复烧未缓存 token,死循环断在这里。
- 批量任务应捕获 SessionLimitError → 立即中止整批 + 落断点,重置后从断点续(完成的
  item 靠 status 永不重跑)。
"""
import json
import re
import subprocess
import sys
import tempfile
from datetime import datetime, timedelta
from pathlib import Path

import yaml

ROOT = Path(__file__).resolve().parents[2]
SETTINGS = yaml.safe_load((ROOT / "config" / "settings.yaml").read_text(encoding="utf-8"))
STATE_FILE = ROOT / "data" / "llm_state.json"
SKILLS_DIR = ROOT / ".claude" / "skills"


def load_prompt(skill: str, key: str = "main") -> str:
    """读 .claude/skills/<skill>/SKILL.md 里 `<!-- prompt:key -->` 标记后那段代码块,
    当 prompt 模板返回(.format 占位由调用方填)。

    prompt = 节点的"智能",搬进 skill 当唯一真相;脚本只取数据 + 拼模板 + 调 call_llm,
    不再内嵌 PROMPT。一个 skill 可含多块(map/reduce、洗稿/原创…),按 key 取。
    """
    text = (SKILLS_DIR / skill / "SKILL.md").read_text(encoding="utf-8")
    marker = f"<!-- prompt:{key} -->"
    i = text.find(marker)
    if i < 0:
        raise RuntimeError(f"skill[{skill}] 缺 prompt 块 `{key}`")
    fence = text.find("```", i)                       # 标记后第一个代码块
    start = text.find("\n", fence) + 1                # 跳过 ```lang 那行
    end = text.find("```", start)
    if fence < 0 or end < 0:
        raise RuntimeError(f"skill[{skill}] 的 `{key}` 块没闭合代码围栏")
    return text[start:end].rstrip("\n")
_MODEL_MAP = {"claude-haiku": "haiku", "claude-sonnet": "sonnet", "claude-opus": "opus"}

# 撞上限的特征串(claude -p 报错文本,大小写不敏感)
_LIMIT_MARKERS = ("session limit", "usage limit", "hit your", "rate limit")
# "resets 8:40am" / "resets 11pm" → 下一个该时刻
_RESET_RE = re.compile(r"reset[s]?\s+(\d{1,2})(?::(\d{2}))?\s*(am|pm)?", re.IGNORECASE)


class SessionLimitError(RuntimeError):
    """订阅会话上限。reset_at = 预计可重试的时间(本地)。"""

    def __init__(self, reset_at: datetime, raw: str = ""):
        self.reset_at = reset_at
        super().__init__(f"订阅会话上限,预计 {reset_at:%H:%M} 恢复 | {raw[:200]}")


def _load_cooldown() -> datetime | None:
    if not STATE_FILE.exists():
        return None
    try:
        v = json.loads(STATE_FILE.read_text(encoding="utf-8")).get("cooldown_until")
        return datetime.fromisoformat(v) if v else None
    except Exception:
        return None


def _save_cooldown(until: datetime) -> None:
    STATE_FILE.parent.mkdir(parents=True, exist_ok=True)
    STATE_FILE.write_text(json.dumps({"cooldown_until": until.isoformat()},
                                     ensure_ascii=False), encoding="utf-8")


def _parse_reset(text: str) -> datetime:
    """从报错里解析重置时刻;解析不到则保守冷却 1 小时(不永久锁死)。"""
    now = datetime.now()
    m = _RESET_RE.search(text or "")
    if not m:
        return now + timedelta(hours=1)
    hour, minute, ampm = int(m.group(1)), int(m.group(2) or 0), (m.group(3) or "").lower()
    if ampm == "pm" and hour != 12:
        hour += 12
    elif ampm == "am" and hour == 12:
        hour = 0
    reset = now.replace(hour=hour % 24, minute=minute, second=0, microsecond=0)
    if reset <= now:                       # 已过 → 指明日同一时刻
        reset += timedelta(days=1)
    return reset


def call_llm(node: str, prompt: str, allowed_tools: list[str] | None = None,
             timeout: int = 600) -> str:
    """按节点路由调用 LLM,返回纯文本输出。

    失败抛 RuntimeError;撞会话上限抛 SessionLimitError(批量任务据此熔断)。
    """
    # 熔断器:仍在冷却期 → 直接 fail-fast,不 spawn、不重读 prompt
    cooldown = _load_cooldown()
    if cooldown and datetime.now() < cooldown:
        raise SessionLimitError(cooldown, "仍在会话上限冷却期(熔断,未发起调用)")

    route = SETTINGS["llm"]["routes"].get(node, SETTINGS["llm"]["default"])
    model = _MODEL_MAP.get(route, route)
    cmd = ["claude", "-p", "--model", model]
    if allowed_tools:
        cmd += ["--allowedTools", ",".join(allowed_tools)]
    r = subprocess.run(cmd, input=prompt, capture_output=True, text=True,
                       encoding="utf-8", errors="replace", timeout=timeout,
                       cwd=tempfile.gettempdir(), shell=(sys.platform == "win32"))
    blob = f"{r.stderr or ''}\n{r.stdout or ''}"

    if any(mark in blob.lower() for mark in _LIMIT_MARKERS):
        reset = _parse_reset(blob)
        _save_cooldown(reset)              # 记冷却 → 同批/后续调用立即熔断
        raise SessionLimitError(reset, blob)

    if r.returncode != 0 or not (r.stdout or "").strip():
        raise RuntimeError(f"LLM节点[{node}/{model}]失败: {(r.stderr or r.stdout or '')[:300]}")
    return r.stdout.strip()
