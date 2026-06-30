"""登记 MVP 起步数据(可重复跑):自营账号 张芝士。"""
import sqlite3
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
sys.stdout.reconfigure(encoding="utf-8")

conn = sqlite3.connect(ROOT / "data" / "creation.db")
# 清掉可能因控制台编码写坏的行
conn.execute("DELETE FROM accounts WHERE name != '张芝士'")
conn.execute(
    "INSERT OR IGNORE INTO accounts(name, domain, platform, persona_path) "
    "VALUES('张芝士', '泛科普', 'douyin', 'vault/人设/张芝士.md')"
)
conn.commit()
print(list(conn.execute("SELECT id, name, domain, platform FROM accounts")))
conn.close()
