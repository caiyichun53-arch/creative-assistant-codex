"""语感燃料原料写入 Obsidian 的通用件。

边界:
- 写入的是原料层 language_fuel_corpus,不是已晋升范例。
- 平台适配器负责采集/清洗/入库;这里只统一 Obsidian frontmatter 和正文格式。
- 适配短评论、长评、回答、文章等不同 source_kind。
"""
from __future__ import annotations

import json
import re
from datetime import datetime
from pathlib import Path
from typing import Any

INVALID_FILENAME = re.compile(r'[\\/:*?"<>|]')


def safe_name(text: str, fallback: str) -> str:
    text = INVALID_FILENAME.sub("", text)
    text = re.sub(r"\s+", " ", text).strip(" ._-")
    return text[:80] or fallback


def md_scalar(value: str | int | None) -> str:
    if value is None:
        return '""'
    return json.dumps(str(value), ensure_ascii=False)


def md_list(values: list[str]) -> str:
    if not values:
        return "[]"
    return "\n".join(f"  - {md_scalar(v)}" for v in values)


def write_language_fuel_note(
    *,
    vault_root: Path,
    vault_dir: str,
    platform: str,
    platform_name: str,
    domain: str,
    domain_name: str,
    source: str,
    source_kind: str,
    item_id: str,
    title: str,
    subtitle: str | None,
    url: str,
    items: list[dict[str, Any]],
    total_items: int | None,
    sample_heading: str = "原料样本",
    extra_frontmatter: dict[str, Any] | None = None,
    extra_sections: list[str] | None = None,
) -> Path:
    folder = vault_root / vault_dir / domain_name / platform_name
    folder.mkdir(parents=True, exist_ok=True)
    note_path = folder / f"{safe_name(f'{title}_{item_id}', item_id)}.md"
    collected_at = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    extra_frontmatter = extra_frontmatter or {}

    lines = [
        "---",
        "type: language_fuel_corpus",
        "status: raw_material",
        f"source: {source}",
        f"source_kind: {source_kind}",
        f"platform: {platform}",
        f"platform_name: {platform_name}",
        f"domain: {domain}",
        f"domain_name: {domain_name}",
        "primary_usage: writing_experience_fuel",
        "promote_to_example: false",
        f"item_id: {md_scalar(item_id)}",
        f"title: {md_scalar(title)}",
        f"subtitle: {md_scalar(subtitle)}",
        f"url: {md_scalar(url)}",
        f"total_items: {total_items if total_items is not None else 'null'}",
        f"kept_count: {len(items)}",
        f"collected_at: {md_scalar(collected_at)}",
    ]
    for key, value in extra_frontmatter.items():
        if isinstance(value, list):
            lines.append(f"{key}:")
            lines.append(md_list([str(v) for v in value]))
        elif isinstance(value, bool):
            lines.append(f"{key}: {str(value).lower()}")
        elif isinstance(value, (int, float)):
            lines.append(f"{key}: {value}")
        elif value is None:
            lines.append(f"{key}: null")
        else:
            lines.append(f"{key}: {md_scalar(value)}")
    lines.extend(
        [
            "tags:",
            "  - 语感燃料",
            f"  - {domain_name}",
            f"  - {platform_name}",
            "---",
            "",
            f"# {title} 语感燃料",
            "",
            "> 原料层笔记:用于后续写作经验提炼;不是已验证范例,不要直接注入写手 prompt。",
            "",
            "## 平台属性",
            f"- 平台: {platform_name} (`{platform}`)",
            f"- 领域: {domain_name}",
            f"- 原料类型: {source_kind}",
            "- 主用途: 写作经验提炼燃料",
            f"- 对象: {title}",
            f"- 补充: {subtitle or ''}",
            f"- 链接: {url}",
            "",
        ]
    )
    if extra_sections:
        lines.extend(extra_sections)
        lines.append("")
    lines.append(f"## {sample_heading}")
    for i, item in enumerate(items, start=1):
        text = str(item.get("content") or "").replace("\r", " ").strip()
        item_title = item.get("title") or item.get("item_title") or ""
        author = item.get("author") or item.get("user_nickname") or ""
        item_time = item.get("time") or item.get("comment_time") or ""
        like_count = item.get("like_count", item.get("liked_count", 0))
        item_type = item.get("item_type") or item.get("comment_type") or source_kind
        platform_item_id = item.get("platform_item_id") or item.get("platform_comment_id") or item.get("id") or ""
        item_url = item.get("url") or ""
        title_suffix = f" · {item_title}" if item_title else ""
        lines.extend(
            [
                "",
                f"### {i}. {item_type}{title_suffix} · {like_count} 赞",
                f"- 作者/用户: {author}",
                f"- 时间: {item_time}",
                f"- 平台内ID: {platform_item_id}",
            ]
        )
        if item_url:
            lines.append(f"- 链接: {item_url}")
        lines.extend(["", f"> {text}"])
    note_path.write_text("\n".join(lines) + "\n", encoding="utf-8")
    return note_path
