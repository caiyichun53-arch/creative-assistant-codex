"""批量完整注册:读 config/domains/<domain>.yaml 的 competitor_seeds,
逐个走 register_full(写表→全量采集→基线判定);已注册的自动跳过,单个失败不中断。

用法: python scripts/collect/register_seeds.py [--domain 泛科普] [--show]
"""
import argparse
import subprocess
import sys
from pathlib import Path

import yaml

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "scripts" / "collect"))
sys.stdout.reconfigure(encoding="utf-8")
from common import connect, parse_sec_uid  # noqa: E402

PY = sys.executable
REGISTER_FULL = ROOT / "scripts" / "collect" / "register_full.py"


def main(domain: str, show: bool) -> None:
    cfg = yaml.safe_load((ROOT / "config" / "domains" / f"{domain}.yaml").read_text(encoding="utf-8"))
    seeds = [s for s in cfg.get("competitor_seeds", []) if isinstance(s, dict) and s.get("url")]

    conn = connect()
    registered = {r[0] for r in conn.execute(
        "SELECT platform_uid FROM competitor_accounts WHERE platform='douyin'")}
    conn.close()

    todo = [s for s in seeds if parse_sec_uid(s["url"]) not in registered]
    skip = [s["name"] for s in seeds if parse_sec_uid(s["url"]) in registered]
    print(f"种子 {len(seeds)} | 已注册跳过 {len(skip)} {skip} | 待注册 {len(todo)}", flush=True)

    ok, fail = [], []
    for i, s in enumerate(todo, 1):
        print(f"\n############ [{i}/{len(todo)}] 完整注册 {s['name']} ############", flush=True)
        cmd = [PY, str(REGISTER_FULL), s["url"], "--name", s["name"], "--domain", domain]
        if show:
            cmd.append("--show")
        r = subprocess.run(cmd)
        (ok if r.returncode == 0 else fail).append(s["name"])

    print("\n===== 批量完整注册结束 =====", flush=True)
    print(f"✅ 成功 {len(ok)}: {ok}")
    if fail:
        print(f"❌ 失败 {len(fail)}: {fail}(可单独重跑 register_full.py)")


if __name__ == "__main__":
    p = argparse.ArgumentParser()
    p.add_argument("--domain", default="泛科普")
    p.add_argument("--show", action="store_true", help="登录态失效时弹浏览器扫码")
    a = p.parse_args()
    main(a.domain, a.show)
