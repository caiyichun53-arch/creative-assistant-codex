"""创建创作领域和自营账号的确定性初始化脚本。

只做初始化:
- 写领域配置
- 写账号配置
- 写人设文件
- 创建经验库基础目录
- 登记 accounts 表

不注册对标账号,不采集视频,不判定爆款,不写稿。
"""
from __future__ import annotations

import argparse
import sqlite3
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]


def fail(message: str) -> None:
    raise SystemExit(message)


def ensure_safe_name(value: str, label: str) -> str:
    value = value.strip()
    if not value:
        fail(f"{label}不能为空")
    bad = set('\\/:*?"<>|')
    if any(ch in bad for ch in value):
        fail(f"{label}不能包含 Windows 路径非法字符: {value}")
    return value


def write_text_once(path: Path, text: str, force: bool) -> str:
    if path.exists() and not force:
        return f"已存在: {path.relative_to(ROOT)}"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8", newline="\n")
    return f"已写入: {path.relative_to(ROOT)}"


def build_domain_yaml(args: argparse.Namespace) -> str:
    return f"""# 领域 = 数据不是代码。新建领域先生成配置和空经验桶,后续经验靠采集、拆解、改稿和发布数据增长。
name: {args.domain}
description: {args.description}
audience: {args.audience}

# 字数范围是写作验收口径。script_target_chars 是区间中点,仅用于兼容现有材料包和创作脚本。
script_target_chars: {args.target_chars}
script_chars_range: [{args.range_min}, {args.range_max}]

# 对标账号种子清单由后续“规划对标账号/注册对标账号”步骤写入。
competitor_seeds:
  []
"""


def build_account_yaml(args: argparse.Namespace, persona_path: str) -> str:
    lines = [
        "# 账号 = 数据不是代码。人设/文风是账号独有,选题/钩子/结构范例随领域共享。",
        f"name: {args.account}",
        f"domain: {args.domain}",
        f"platform: {args.platform}",
    ]
    if args.profile_url:
        lines.append(f"profile_url: {args.profile_url}")
    lines.append(f"persona: {persona_path}")
    lines.append("status: active")
    return "\n".join(lines) + "\n"


def build_persona(args: argparse.Namespace) -> str:
    return f"""# {args.account}

## 账号定位

- 领域:{args.domain}
- 平台:{args.platform}
- 内容方向:{args.description}
- 目标受众:{args.audience}

## 人设材料

{args.persona_text.strip()}

## 使用边界

- 本文件只定义账号人设和表达取向。
- 禁用词、硬约束和风险校验不写进本文件。
- 文风经验以后来自认可稿、改稿对照和正式晋升范例。
"""


def ensure_vault_dirs(domain: str) -> list[str]:
    paths = [
        ROOT / "vault" / "人设",
        ROOT / "vault" / "范例" / "选题",
        ROOT / "vault" / "范例" / "钩子",
        ROOT / "vault" / "范例" / "结构",
        ROOT / "vault" / "范例" / "文风",
        ROOT / "vault" / "语感燃料" / domain,
    ]
    result = []
    for path in paths:
        path.mkdir(parents=True, exist_ok=True)
        result.append(f"目录就绪: {path.relative_to(ROOT)}")
    return result


def upsert_account(args: argparse.Namespace, persona_path: str) -> str:
    db = ROOT / "data" / "creation.db"
    if not db.exists():
        fail(f"数据库不存在: {db.relative_to(ROOT)}。请先初始化数据库。")
    conn = sqlite3.connect(db)
    try:
        conn.execute(
            """INSERT INTO accounts(name, domain, platform, persona_path, status)
               VALUES(?, ?, ?, ?, 'active')
               ON CONFLICT(name) DO UPDATE SET
                 domain=excluded.domain,
                 platform=excluded.platform,
                 persona_path=excluded.persona_path,
                 status='active'""",
            (args.account, args.domain, args.platform, persona_path),
        )
        conn.commit()
        row = conn.execute(
            "SELECT id, name, domain, platform, persona_path FROM accounts WHERE name=?",
            (args.account,),
        ).fetchone()
        if row is None:
            fail("账号写入后未能从 accounts 表读回")
        return (
            "数据库账号就绪: "
            f"id={row[0]} name={row[1]} domain={row[2]} platform={row[3]} persona={row[4]}"
        )
    finally:
        conn.close()


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--domain", required=True, help="领域名称,例如 音乐")
    parser.add_argument("--account", required=True, help="账号名称")
    parser.add_argument("--platform", default="douyin", choices=["douyin"], help="平台,当前只支持 douyin")
    parser.add_argument("--description", required=True, help="内容方向")
    parser.add_argument("--audience", required=True, help="目标受众")
    parser.add_argument("--target-chars", type=int, help="建议中心字数。未传入时自动取字数范围中点")
    parser.add_argument("--range-min", type=int, required=True, help="字数下限")
    parser.add_argument("--range-max", type=int, required=True, help="字数上限")
    parser.add_argument("--persona-text", required=True, help="人设材料")
    parser.add_argument("--profile-url", default="", help="账号主页链接")
    parser.add_argument("--force", action="store_true", help="允许覆盖同名配置和人设文件")
    args = parser.parse_args()

    args.domain = ensure_safe_name(args.domain, "领域名称")
    args.account = ensure_safe_name(args.account, "账号名称")
    if args.range_min > args.range_max:
        fail("字数下限不能大于字数上限")
    if args.target_chars is None:
        args.target_chars = round((args.range_min + args.range_max) / 2)
    if not (args.range_min <= args.target_chars <= args.range_max):
        fail("目标字数必须落在字数范围内")

    persona_path = f"vault/人设/{args.account}.md"
    outputs: list[str] = []
    outputs.append(write_text_once(ROOT / "config" / "domains" / f"{args.domain}.yaml", build_domain_yaml(args), args.force))
    outputs.append(write_text_once(ROOT / "config" / "accounts" / f"{args.account}.yaml", build_account_yaml(args, persona_path), args.force))
    outputs.append(write_text_once(ROOT / persona_path, build_persona(args), args.force))
    outputs.extend(ensure_vault_dirs(args.domain))
    outputs.append(upsert_account(args, persona_path))

    print("新建领域账号完成")
    for line in outputs:
        print(f"- {line}")


if __name__ == "__main__":
    sys.stdout.reconfigure(encoding="utf-8")
    main()
