"""原创开题(题目优先备料通路·入口):从一个题目/方向直接建选题,不挂爆款。

对照 daily_topics(从爆款造选题),本脚本是"我想写 X"的从零入口:
插一行 source_type='original' 的选题(不挂 hit、直接 selected),随即跑 prepare_topic
备料(无 hit 时 research 走原创分支、assemble 跳过来源爆款段)。衍生方向(spinoff)以后接。

用法:
  python scripts/topics/new_topic.py --title "为什么夏天的西瓜更甜" --domain 泛科普
  python scripts/topics/new_topic.py --title "..." --domain 泛科普 --angle "从糖分运输讲" --no-prep
"""
import argparse
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "scripts" / "collect"))
sys.stdout.reconfigure(encoding="utf-8", errors="replace")
from common import connect  # noqa: E402

PY = sys.executable


def create(title: str, domain: str, angle: str | None) -> int:
    """插一行原创选题(直接 selected),返回新 id。"""
    conn = connect()
    with conn:
        cur = conn.execute(
            """INSERT INTO topics(title, angle, domain, source_type, source_hit_id,
                                  status, selected_at)
               VALUES(?,?,?, 'original', NULL, 'selected', datetime('now','localtime'))""",
            (title.strip(), (angle or None), domain.strip()))
    tid = cur.lastrowid
    conn.close()
    return tid


def main() -> None:
    p = argparse.ArgumentParser(description="原创开题:从题目直接建选题并备料")
    p.add_argument("--title", required=True, help="选题标题(你想写什么)")
    p.add_argument("--domain", required=True, help="领域(对应 config/domains/<领域>.yaml)")
    p.add_argument("--angle", help="切入角度(可选)")
    p.add_argument("--no-prep", action="store_true", help="只建选题、不自动备料")
    a = p.parse_args()

    dom_file = ROOT / "config" / "domains" / f"{a.domain}.yaml"
    if not dom_file.exists():
        print(f"⚠️ 领域配置 {dom_file.relative_to(ROOT)} 不存在(篇幅会按默认);确认领域名无误再继续。")

    tid = create(a.title, a.domain, a.angle)
    print(f"✅ 原创选题已建: [{tid}] {a.title}(领域 {a.domain}, source_type=original)")

    if a.no_prep:
        print(f"下一步备料: python scripts/topics/prepare_topic.py {tid}")
        return
    print(f"--- 开始备料(prepare_topic {tid})")
    r = subprocess.run([PY, str(ROOT / "scripts" / "topics" / "prepare_topic.py"), str(tid)], cwd=ROOT)
    sys.exit(r.returncode)


if __name__ == "__main__":
    main()
