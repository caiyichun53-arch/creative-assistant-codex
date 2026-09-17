"""Read-only knowledge audit; the retired apply operation is rejected."""

from __future__ import annotations

import argparse
import json
import re
import sys
from pathlib import Path
from typing import Any

from scripts.core.production.stage0_content_core import FORMAL_DB_PATH, Stage0ContentProductionCore


VAULT_ROOT = Path(r"I:\Obsidian\创作助手\经验库")


def _rows(core: Stage0ContentProductionCore, sql: str, params: tuple[Any, ...] = ()) -> list[dict[str, Any]]:
    return [{key: row[key] for key in row.keys()} for row in core.conn.execute(sql, params).fetchall()]


def _count_by(rows: list[dict[str, Any]], key: str) -> dict[str, int]:
    result: dict[str, int] = {}
    for row in rows:
        value = str(row.get(key) or "未记录")
        result[value] = result.get(value, 0) + 1
    return dict(sorted(result.items()))


def audit(
    core: Stage0ContentProductionCore,
    *,
    include_samples: bool = False,
    include_schema: bool = False,
) -> dict[str, Any]:
    identity = core.data_identity
    accounts = _rows(
        core,
        "SELECT content_account_id, account_role, display_name, domain_label, status "
        "FROM stage0_content_account WHERE data_identity=? ORDER BY account_role, display_name",
        (identity,),
    )
    collection_accounts = _rows(
        core,
        "SELECT account_id, account_name, domain_label, registration_status "
        "FROM competitor_accounts ORDER BY account_name, account_id",
    )
    question_sources = _rows(
        core,
        "SELECT expansion_id, domain_label, core_question, validation_outcome, validated_at "
        "FROM stage1_question_expansion_source WHERE data_identity=? ORDER BY validated_at, expansion_id",
        (identity,),
    )
    candidates = _rows(
        core,
        "SELECT candidate_version_id, candidate_id, domain_label, status, created_at "
        "FROM stage1b_candidate_version WHERE data_identity=? ORDER BY created_at, candidate_version_id",
        (identity,),
    )
    candidate_pool = _rows(
        core,
        "SELECT candidate_version_id, pool_status, effective_at FROM stage1b_candidate_pool_state "
        "WHERE data_identity=? ORDER BY effective_at, pool_state_id",
        (identity,),
    )
    experience_candidates = _rows(
        core,
        "SELECT experience_candidate_id, task_id, experience_candidate_run_id, domain_label, status, created_at "
        "FROM stage0_experience_candidate WHERE data_identity=? ORDER BY created_at, experience_candidate_id",
        (identity,),
    )
    experience_runs = _rows(
        core,
        "SELECT experience_candidate_run_id, domain_label, status, created_at, completed_at "
        "FROM stage0_experience_candidate_run WHERE data_identity=? ORDER BY created_at, experience_candidate_run_id",
        (identity,),
    )
    tasks = _rows(
        core,
        "SELECT task_id, current_node, current_status, current_version_id, created_at "
        "FROM stage0_content_task WHERE data_identity=? ORDER BY created_at, task_id",
        (identity,),
    )
    task_summary = []
    for item in core.list_content_workbench():
        topic = item.get("topic") if isinstance(item.get("topic"), dict) else {}
        if isinstance(topic.get("payload"), dict):
            topic = topic["payload"]
        task_summary.append({
            "task_id": item.get("task_id"),
            "title": topic.get("title") or topic.get("core_question") or "未命名",
            "account_ref": topic.get("account_ref") or topic.get("account_id") or "",
            "domain_label": topic.get("domain_label") or topic.get("domain") or "",
            "current_node": item.get("current_node"),
            "current_status": item.get("current_status"),
            "created_at": item.get("created_at"),
        })
    publications = _rows(
        core,
        "SELECT publication_id, task_id, domain_label, status, created_at "
        "FROM stage0_publication_registration WHERE data_identity=? ORDER BY created_at, publication_id",
        (identity,),
    )
    tags = _rows(
        core,
        "SELECT tag_id, tag, domain_label, status, human_review_status FROM domain_search_tags "
        "ORDER BY domain_label, tag, tag_id",
    )
    videos = _rows(
        core,
        "SELECT video.video_id, video.account_id, account.account_name, account.registration_status, "
        "video.excluded_reason, video.title FROM competitor_videos video "
        "LEFT JOIN competitor_accounts account ON account.account_id=video.account_id "
        "ORDER BY account.account_name, video.video_id",
    )
    old_root = VAULT_ROOT / "自媒体内容创作知识库"
    old_top = []
    if old_root.exists():
        old_top = sorted(item.name for item in old_root.iterdir() if item.is_dir())
    foreign_keys: dict[str, list[dict[str, Any]]] = {}
    table_rows = core.conn.execute(
        "SELECT name FROM sqlite_master WHERE type='table' AND name NOT LIKE 'sqlite_%' ORDER BY name"
    ).fetchall()
    for table_row in table_rows:
        table_name = str(table_row["name"])
        foreign_keys[table_name] = [
            {"table": str(fk[2]), "from": str(fk[3]), "to": str(fk[4])}
            for fk in core.conn.execute(f'PRAGMA foreign_key_list("{table_name}")').fetchall()
        ]
    result = {
        "data_identity": identity,
        "counts": {
            "formal_accounts": len(accounts),
            "collection_accounts": len(collection_accounts),
            "question_sources": len(question_sources),
            "candidates": len(candidates),
            "experience_candidates": len(experience_candidates),
            "experience_runs": len(experience_runs),
            "tasks": len(tasks),
            "publications": len(publications),
            "tags": len(tags),
            "videos": len(videos),
        },
        "account_status": _count_by(accounts, "status"),
        "account_role_status": _count_by(
            [{"value": f"{row.get('account_role')}｜{row.get('status')}"} for row in accounts], "value"
        ),
        "collection_status": _count_by(collection_accounts, "registration_status"),
        "question_domains": _count_by(question_sources, "domain_label"),
        "candidate_status": _count_by(candidates, "status"),
        "experience_candidate_status": _count_by(experience_candidates, "status"),
        "experience_run_status": _count_by(experience_runs, "status"),
        "task_status": _count_by(tasks, "current_status"),
        "task_summary": task_summary,
        "publication_status": _count_by(publications, "status"),
        "tag_status": _count_by(tags, "status"),
        "disabled_or_excluded_videos": [
            row for row in videos if row.get("registration_status") != "active" or row.get("excluded_reason")
        ][:50],
        "knowledge_base_top_directories": old_top,
    }
    if include_schema:
        result["foreign_keys"] = foreign_keys
    if include_samples:
        result["sample_question_sources"] = question_sources[:30]
        result["sample_experience_candidates"] = experience_candidates[:30]
    return result


def verify(core: Stage0ContentProductionCore) -> dict[str, Any]:
    registry = core.list_knowledge_account_registry()
    source_records = core.list_knowledge_source_records()
    selection = core.list_knowledge_selection_inputs()
    candidates = core.list_knowledge_candidate_pool()
    experience = core.list_pre_topic_experience_candidates()
    workbench = core.list_content_workbench()
    mirror_root = VAULT_ROOT / "自媒体内容创作知识库"
    expected_top = {
        "00 导航", "01 账号与领域", "02 素材", "03 选题", "04 自营内容",
        "05 风格与偏好", "06 经验", "07 系统记录", "08 归档",
    }
    top_dirs = {
        item.name for item in mirror_root.iterdir() if item.is_dir()
    } if mirror_root.exists() else set()
    stale_names = {
        "历史候选", "项目基础", "整理材料", "内容依据", "备料",
        "来源与基础信息",
    }
    stale_dirs = [
        str(item) for item in mirror_root.rglob("*")
        if item.is_dir() and item.name in stale_names
    ] if mirror_root.exists() else []
    link_missing: list[str] = []
    link_count = 0
    link_prefix = "创作助手/经验库/自媒体内容创作知识库/"
    if mirror_root.exists():
        for page in mirror_root.rglob("*.md"):
            try:
                text = page.read_text(encoding="utf-8")
            except (OSError, UnicodeError):
                continue
            for match in re.finditer(r"\[\[([^\]|]+)(?:\|[^\]]+)?\]\]", text):
                link_count += 1
                target = match.group(1)
                if not target.startswith(link_prefix):
                    continue
                relative = Path(target[len(link_prefix):].replace("/", "\\"))
                candidate = mirror_root / relative
                if candidate.exists() or Path(str(candidate) + ".md").exists() or (candidate / "index.md").exists():
                    continue
                link_missing.append(f"{page}:{target}")
    source_ids = [str(item.get("source_id") or "") for item in source_records]
    source_domains = {str(item.get("domain_label") or "") for item in source_records}
    account_domains = {
        str(item.get("domain_label") or "")
        for item in (registry.get("formal_accounts") or [])
        if isinstance(item, dict)
    }
    pending_run_ids = {str(item.get("experience_candidate_run_id") or "") for item in experience}
    writing_nodes = {"content_plan", "formal_draft", "copy_optimization", "de_ai_revision", "review"}
    research_leaked_to_self_content = any(
        str(item.get("current_node") or "") not in writing_nodes
        and (item.get("current_node") is not None)
        and bool(item.get("audio_attempts"))
        for item in workbench
    )
    checks = {
        "只有有效账号": all(str(item.get("status") or "") == "active" for item in registry.get("formal_accounts") or []),
        "采集账号属于有效对标账号": all(
            str(item.get("account_id") or "") in {
                str(account.get("content_account_id") or "")
                for account in registry.get("formal_accounts") or []
                if account.get("account_role") == "competitor"
            }
            for item in registry.get("collection_accounts") or []
        ),
        "来源编号不重复": len(source_ids) == len(set(source_ids)),
        "来源领域来自账号领域": source_domains.issubset(account_domains),
        "没有旧选题输入混入": not (selection.get("question_sources") or selection.get("hotspots") or selection.get("discovered_videos")),
        "候选选题只来自正式候选表": all(str(item.get("domain_label") or "") != "通用" for item in candidates),
        "经验候选只来自当前运行": len(pending_run_ids) <= 1,
        "没有研究结果流入自营内容": not research_leaked_to_self_content,
        "知识库顶层结构唯一": top_dirs == expected_top,
        "没有旧目录": not stale_dirs,
        "内部链接全部可达": not link_missing,
    }
    return {
        "status": "passed" if all(checks.values()) else "failed",
        "checks": checks,
        "counts": {
            "active_formal_accounts": len(registry.get("formal_accounts") or []),
            "active_collection_accounts": len(registry.get("collection_accounts") or []),
            "source_records": len(source_records),
            "experience_candidates": len(experience),
            "formal_tasks": len(workbench),
            "internal_links_checked": link_count,
        },
        "failures": stale_dirs + link_missing,
    }


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--action", choices=("audit", "verify", "apply"), default="audit")
    parser.add_argument("--actor", default="Codex")
    parser.add_argument("--show-samples", action="store_true")
    parser.add_argument("--show-schema", action="store_true")
    args = parser.parse_args()
    if args.action == "apply":
        raise SystemExit(
            "the legacy Hermes knowledge convergence apply route is retired; "
            "use the Creation Assistant Core formal entry"
        )
    core = Stage0ContentProductionCore.open_read_only(FORMAL_DB_PATH, data_identity="production")
    try:
        if args.action == "audit":
            sys.stdout.buffer.write(
                (json.dumps(audit(core, include_samples=args.show_samples, include_schema=args.show_schema), ensure_ascii=False, indent=2, default=str) + "\n").encode("utf-8")
            )
            return 0
        sys.stdout.buffer.write(
            (json.dumps(verify(core), ensure_ascii=False, indent=2, default=str) + "\n").encode("utf-8")
        )
        return 0
    finally:
        core.close()


if __name__ == "__main__":
    raise SystemExit(main())
