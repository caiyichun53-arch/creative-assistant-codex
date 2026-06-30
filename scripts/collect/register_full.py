"""一键完整注册对标账号:① 写表 → ② 全量采集存量快照 → ③ 算基线+判爆款。

注册 ≠ 裸写库:单加一个对标账号也要走完整套(register → crawl --mode full → judge_hits)。
这是 register_competitor.py(只写表)的完整版,补齐"注册=完整流程"。

用法:
  python scripts/collect/register_full.py <主页链接|短链|sec_uid> [--name 名字] [--domain 泛科普] [--show]
  --show  首次扫码 / 登录态失效时弹浏览器窗口(默认无头)。

批量:register_seeds.py 读 config 种子清单循环调用本脚本。
"""
import argparse
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "scripts" / "collect"))
sys.stdout.reconfigure(encoding="utf-8")
from common import connect, parse_sec_uid  # noqa: E402

PY = sys.executable
COLLECT = ROOT / "scripts" / "collect"
ANALYZE = ROOT / "scripts" / "analyze"


def run_step(title: str, cmd: list[str]) -> None:
    print(f"\n===== {title} =====", flush=True)
    r = subprocess.run(cmd)
    if r.returncode != 0:
        sys.exit(f"✗ [{title}] 失败 (exit {r.returncode}),完整注册中止")
    print(f"✓ {title} 完成", flush=True)


def register_full(url: str, name: str | None, domain: str, show: bool) -> None:
    sec_uid = parse_sec_uid(url)
    if not sec_uid:
        sys.exit(f"无法从输入解析 sec_uid: {url}")

    run_step("① 注册账号(写表)",
             [PY, str(COLLECT / "register_competitor.py"), url,
              *(["--name", name] if name else []), "--domain", domain])

    conn = connect()
    row = conn.execute(
        "SELECT id, name FROM competitor_accounts WHERE platform='douyin' AND platform_uid=?",
        (sec_uid,)).fetchone()
    conn.close()
    if not row:
        sys.exit("注册后在 competitor_accounts 查不到该账号,异常中止")
    cid = row["id"]

    crawl_cmd = [PY, str(COLLECT / "crawl_competitors.py"), "--ids", str(cid), "--mode", "full"]
    if show:
        crawl_cmd.append("--show")
    run_step("② 全量采集存量快照(--mode full)", crawl_cmd)

    run_step("③ 算基线 + 判爆款", [PY, str(ANALYZE / "judge_hits.py"), "--ids", str(cid)])

    print(f"\n✅ 完整注册完成: [{cid}] {row['name']}")


if __name__ == "__main__":
    p = argparse.ArgumentParser()
    p.add_argument("url", help="主页链接 / 短链 / sec_uid")
    p.add_argument("--name", default=None)
    p.add_argument("--domain", default="泛科普")
    p.add_argument("--show", action="store_true", help="首次扫码 / 登录态失效时弹浏览器窗口")
    a = p.parse_args()
    register_full(a.url, a.name, a.domain, a.show)
