"""登记成稿(确定性):data/topics/<id>/draft_v<N>.md → drafts 表。

创作对话写完成稿后调用;稿件正文由创作对话写入文件,本脚本只登记。
用法: python scripts/content/save_draft.py <topic_id> [--stage full] [--author ai]
约定: 创作对话把成稿写到 data/topics/<id>/draft_v1.md(或递增版本)。
"""
import argparse
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "scripts" / "collect"))
sys.stdout.reconfigure(encoding="utf-8", errors="replace")
from common import connect  # noqa: E402


def main() -> None:
    p = argparse.ArgumentParser()
    p.add_argument("topic_id", type=int)
    p.add_argument("--stage", default="full")
    p.add_argument("--author", default="ai")
    p.add_argument("--version", type=int, help="登记哪一版(缺省=最新);多版并存时务必指定")
    a = p.parse_args()

    conn = connect()
    t = conn.execute("SELECT domain FROM topics WHERE id=?", (a.topic_id,)).fetchone()
    if not t:
        sys.exit(f"选题 {a.topic_id} 不存在")
    acct = conn.execute("SELECT id FROM accounts WHERE domain=? LIMIT 1",
                        (t["domain"],)).fetchone()

    out_dir = ROOT / "data" / "topics" / str(a.topic_id)
    versions = sorted(out_dir.glob("draft_v*.md"))
    if not versions:
        sys.exit(f"没找到稿件文件:请先把成稿写到 {out_dir}/draft_v1.md")
    if a.version:
        latest = out_dir / f"draft_v{a.version}.md"
        if not latest.exists():
            sys.exit(f"没有 {latest.name}")
    else:
        latest = versions[-1]
    ver = int(latest.stem.split("v")[-1])

    with conn:
        conn.execute(
            """INSERT INTO drafts(topic_id, account_id, version, stage, author, content_path)
               VALUES(?,?,?,?,?,?)""",
            (a.topic_id, acct["id"], ver, a.stage, a.author,
             str(latest.relative_to(ROOT))))
    conn.close()
    print(f"已登记: {latest.relative_to(ROOT)} (v{ver}, {a.stage}, {a.author})")


if __name__ == "__main__":
    main()
