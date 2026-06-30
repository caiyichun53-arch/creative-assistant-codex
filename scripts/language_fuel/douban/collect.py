"""豆瓣语感燃料统一入口。"""
from __future__ import annotations

import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[3]
sys.stdout.reconfigure(encoding="utf-8")

COMMANDS = {
    "reviews": ROOT / "scripts" / "language_fuel" / "douban" / "collect_reviews.py",
    "comments": ROOT / "scripts" / "language_fuel" / "douban" / "collect_comments.py",
    "groups": ROOT / "scripts" / "language_fuel" / "douban" / "collect_group_topics.py",
}


def main() -> int:
    if len(sys.argv) < 2 or sys.argv[1] in {"-h", "--help"}:
        print("用法: collect.py {reviews|comments|groups} [参数]")
        print("  reviews  长评全文")
        print("  comments 短评")
        print("  groups   小组帖子/回复")
        return 0
    kind = sys.argv[1]
    if kind not in COMMANDS:
        print(f"未知类型: {kind}. 可用: {', '.join(sorted(COMMANDS))}", file=sys.stderr)
        return 2
    script = COMMANDS[kind]
    rest = sys.argv[2:]
    cmd = [sys.executable, str(script), *rest]
    return subprocess.run(cmd, cwd=ROOT).returncode


if __name__ == "__main__":
    raise SystemExit(main())
