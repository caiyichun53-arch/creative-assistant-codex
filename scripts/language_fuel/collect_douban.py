"""兼容入口:默认转到豆瓣长评全文采集。

新入口:
  python scripts/language_fuel/douban/collect.py reviews --channel movie --query "罗马，不设防的城市"
  python scripts/language_fuel/douban/collect.py comments --channel book --query "活着"
  python scripts/language_fuel/douban/collect.py groups --topic-url "https://www.douban.com/group/topic/..."
"""
from __future__ import annotations

import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
sys.stdout.reconfigure(encoding="utf-8")


def main() -> int:
    script = ROOT / "scripts" / "language_fuel" / "douban" / "collect_reviews.py"
    return subprocess.run([sys.executable, str(script), *sys.argv[1:]], cwd=ROOT).returncode


if __name__ == "__main__":
    raise SystemExit(main())
