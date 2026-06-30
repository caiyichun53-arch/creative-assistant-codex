"""注册对标账号 → competitor_accounts 表(账号池;视频另入 observation_pool)。

用法:
  python scripts/collect/register_competitor.py <主页链接|短链|sec_uid> [--name 名字] [--domain 泛科普]
  python scripts/collect/register_competitor.py --list           # 看当前账号池
可重复跑:同 sec_uid 再注册只更新名字/链接,不重复建。
"""
import argparse
import sys

sys.stdout.reconfigure(encoding="utf-8")
from common import connect, parse_sec_uid  # noqa: E402


def register(url_or_uid: str, name: str | None, domain: str) -> None:
    sec_uid = parse_sec_uid(url_or_uid)
    if not sec_uid:
        sys.exit(f"无法从输入解析 sec_uid: {url_or_uid}")
    url = f"https://www.douyin.com/user/{sec_uid}"
    conn = connect()
    with conn:
        conn.execute(
            """INSERT INTO competitor_accounts(platform, platform_uid, name, url, domain)
               VALUES('douyin', ?, ?, ?, ?)
               ON CONFLICT(platform, platform_uid) DO UPDATE SET
                 name = COALESCE(excluded.name, name),
                 url  = excluded.url""",
            (sec_uid, name or sec_uid[:16], url, domain),
        )
    row = conn.execute(
        "SELECT id, name, platform_uid FROM competitor_accounts WHERE platform='douyin' AND platform_uid=?",
        (sec_uid,),
    ).fetchone()
    print(f"OK id={row['id']} name={row['name']} sec_uid={row['platform_uid']}")
    conn.close()


def list_all() -> None:
    conn = connect()
    rows = conn.execute(
        "SELECT id, name, domain, status, follower_count, last_crawled_at, platform_uid "
        "FROM competitor_accounts ORDER BY id"
    ).fetchall()
    for r in rows:
        print(f"[{r['id']}] {r['name']} | {r['domain']} | {r['status']} | "
              f"粉丝:{r['follower_count']} | 上次采集:{r['last_crawled_at']} | {r['platform_uid'][:24]}…")
    print(f"共 {len(rows)} 个对标账号")
    conn.close()


if __name__ == "__main__":
    p = argparse.ArgumentParser()
    p.add_argument("url", nargs="?", help="主页链接 / 短链 / sec_uid")
    p.add_argument("--name", default=None)
    p.add_argument("--domain", default="泛科普")
    p.add_argument("--list", action="store_true")
    a = p.parse_args()
    if a.list:
        list_all()
    elif a.url:
        register(a.url, a.name, a.domain)
    else:
        p.print_help()
