#!/usr/bin/env python3
"""Create a topic evidence scaffold from a validated research_pack.

This script does not try to be an editor. It organizes evidence, marks gaps,
and produces reviewable candidate axes that an agent or user can judge.
"""

from __future__ import annotations

import argparse
import json
import re
import sys
from datetime import datetime
from pathlib import Path
from typing import Any


def load_json(path: Path) -> dict[str, Any]:
    return json.loads(path.read_text(encoding="utf-8-sig"))


def write_json(path: Path, data: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")


def clean_text(value: Any, limit: int = 260) -> str:
    text = re.sub(r"\s+", " ", str(value or "")).strip()
    return text[:limit]


def score(material: int, depth: int, risk: int = 4, freshness: int = 3, user_fit: int = 4) -> dict[str, int]:
    return {
        "material_support": max(1, min(5, material)),
        "freshness": max(1, min(5, freshness)),
        "depth": max(1, min(5, depth)),
        "risk_control": max(1, min(5, risk)),
        "user_fit": max(1, min(5, user_fit)),
    }


def status_from_score(values: dict[str, int], missing: list[str]) -> str:
    if missing:
        return "needs_research"
    if values["material_support"] >= 4 and values["depth"] >= 4:
        return "strong"
    if values["material_support"] <= 2:
        return "needs_research"
    return "candidate"


def task_matches(task: dict[str, Any], keywords: list[str]) -> bool:
    haystack = " ".join(
        [
            str(task.get("task_id", "")),
            str(task.get("layer", "")),
            str(task.get("purpose", "")),
        ]
    )
    return any(keyword in haystack for keyword in keywords)


def tasks_by_keywords(pack: dict[str, Any], keywords: list[str]) -> list[dict[str, Any]]:
    return [task for task in pack.get("evidence_by_task", []) if task_matches(task, keywords)]


def layer_count(pack: dict[str, Any], keywords: list[str]) -> int:
    return sum(int(task.get("ok_sources_count") or 0) for task in tasks_by_keywords(pack, keywords))


def snippets_from_tasks(tasks: list[dict[str, Any]], limit: int = 5) -> list[str]:
    snippets: list[str] = []
    for task in tasks:
        for item in task.get("evidence_by_question", []) or []:
            for evidence in item.get("evidence", []) or []:
                for snippet in evidence.get("snippets", []) or []:
                    clean = clean_text(snippet)
                    if clean and clean not in snippets:
                        snippets.append(clean)
                    if len(snippets) >= limit:
                        return snippets
    return snippets


def task_ids(tasks: list[dict[str, Any]]) -> list[str]:
    return [str(task.get("task_id")) for task in tasks if task.get("task_id")]


def domain_collector_gaps(tasks: list[dict[str, Any]], include_keywords: list[str] | None = None) -> list[str]:
    gaps: list[str] = []
    for task in tasks:
        for collector in task.get("domain_collectors", []) or []:
            status = collector.get("status")
            if status and status not in {"done", "not_needed"}:
                gap = str(collector.get("purpose") or f"领域采集器 {collector.get('collector')} 仍未完成")
                if include_keywords is not None:
                    if not include_keywords:
                        continue
                    if not any(keyword in gap for keyword in include_keywords):
                        continue
                if gap not in gaps:
                    gaps.append(gap)
    return gaps


def build_material_profile(pack: dict[str, Any]) -> dict[str, Any]:
    evidence_tasks = pack.get("evidence_by_task", []) or []
    counts = {
        "fact": layer_count(pack, ["fact"]),
        "viewpoint": layer_count(pack, ["viewpoint", "review"]),
        "reception": layer_count(pack, ["reception", "comment"]),
        "domain_fact": layer_count(pack, ["domain_fact", "profile", "works", "song", "album", "work"]),
        "domain_viewpoint": layer_count(pack, ["domain_viewpoint", "reviews"]),
        "domain_reception": layer_count(pack, ["domain_reception", "reception"]),
    }
    weak_layers = [name for name, count in counts.items() if count < 2]
    strongest = sorted(
        [
            {
                "task_id": task.get("task_id"),
                "layer": task.get("layer"),
                "ok_sources_count": int(task.get("ok_sources_count") or 0),
                "purpose": task.get("purpose"),
            }
            for task in evidence_tasks
        ],
        key=lambda item: item["ok_sources_count"],
        reverse=True,
    )[:6]
    return {
        "source_counts": counts,
        "available_task_ids": task_ids(evidence_tasks),
        "strongest_tasks": strongest,
        "weak_layers": weak_layers,
        "research_gaps": pack.get("research_gaps", []),
        "domain_notes": pack.get("domain_notes", []),
    }


FRAME_DEFS = [
    {
        "axis_id": "public_memory_vs_core_value",
        "label": "大众记忆与核心价值是否错位",
        "content_type": "人物/作品深度选题",
        "core_question": "{obj}最容易被大众记住的东西，和真正值得深入讲的价值是否一致？",
        "need_keywords": [["reception", "comment"], ["viewpoint", "review", "works", "song", "album"]],
        "missing_rules": [
            ("缺少大众接受度材料，无法判断普通观众真正记住了什么。", ["reception", "comment"]),
            ("缺少作品或专业评价材料，无法判断所谓核心价值是否成立。", ["viewpoint", "review", "works", "song", "album"]),
        ],
        "risk": ["容易写成粉丝安利，或者把大众记忆直接贬低成误读。"],
        "foundation_items": ["stable_001", "stable_006", "stable_007", "stable_020"],
        "target_source_types": ["platform_reception", "professional_media", "comments"],
        "collector_gap_keywords": ["代表歌曲", "歌曲评论", "接受度", "长评", "短评"],
    },
    {
        "axis_id": "turning_point_or_evolution",
        "label": "关键转折点是否足够清楚",
        "content_type": "人物传记/背景故事",
        "core_question": "重新讲{obj}时，真正能支撑一条主线的转折点在哪里？",
        "need_keywords": [["fact", "profile"], ["viewpoint", "interview", "review"]],
        "missing_rules": [
            ("缺少可核对的事实时间线，容易写成印象式传记。", ["fact", "profile"]),
            ("缺少一手访谈、评论或阶段判断，无法说明为什么这个节点是转折。", ["viewpoint", "interview", "review"]),
        ],
        "risk": ["容易变成百科时间线，阶段之间只有年份，没有因果。"],
        "foundation_items": ["stable_005", "stable_009", "stable_011", "stable_020"],
        "target_source_types": ["official", "interview", "professional_media"],
        "collector_gap_keywords": [],
    },
    {
        "axis_id": "misreading_or_label_correction",
        "label": "公众标签是否需要校正",
        "content_type": "观点纠偏/人物深度",
        "core_question": "关于{obj}，公众最容易误读的地方是什么，它能不能被材料讲清楚？",
        "need_keywords": [["reception", "comment"], ["viewpoint", "review", "fact"]],
        "missing_rules": [
            ("缺少公众标签或误读材料，无法证明这个误读真实存在。", ["reception", "comment"]),
            ("缺少能纠偏的事实或专业材料，容易变成主观辩护。", ["viewpoint", "review", "fact"]),
        ],
        "risk": ["容易变成反向洗白、反向黑稿，仍然被原标签牵着走。"],
        "foundation_items": ["stable_007", "stable_019", "stable_020"],
        "target_source_types": ["comments", "professional_media", "authority_database"],
        "collector_gap_keywords": ["评论", "接受度", "误读"],
    },
    {
        "axis_id": "work_entry_case",
        "label": "能否用具体作品打开对象",
        "content_type": "音乐赏析/作品切入人物",
        "core_question": "有没有一首歌、一张专辑或一个作品，能比简历更好地解释{obj}？",
        "need_keywords": [["works", "song", "album", "work"], ["viewpoint", "review"]],
        "missing_rules": [
            ("缺少代表作品材料，无法确定从哪一个作品切入。", ["works", "song", "album", "work"]),
            ("缺少作品评价或听感材料，容易只列代表作。", ["viewpoint", "review"]),
        ],
        "risk": ["容易停留在代表作盘点，而不是用作品解释人物或主题。"],
        "foundation_items": ["stable_002", "stable_003", "stable_004", "stable_017"],
        "target_source_types": ["official", "music_platform", "review", "comments"],
        "collector_gap_keywords": ["代表歌曲", "歌曲评论", "长评", "短评"],
    },
    {
        "axis_id": "background_necessity",
        "label": "背景信息是否真的必要",
        "content_type": "背景故事/主题分析",
        "core_question": "围绕{obj}的背景材料，哪些能真正改变理解，哪些只是资料堆积？",
        "need_keywords": [["fact", "profile"], ["viewpoint", "review", "background"]],
        "missing_rules": [
            ("缺少背景事实，无法判断哪些信息是必要前提。", ["fact", "profile"]),
            ("缺少解释性材料，无法把背景转成内容主线。", ["viewpoint", "review", "background"]),
        ],
        "risk": ["容易写成资料型长稿，信息可信但没有叙事重心。"],
        "foundation_items": ["stable_018", "stable_019", "stable_020"],
        "target_source_types": ["official", "interview", "professional_media"],
        "collector_gap_keywords": [],
    },
]

TOPIC_DIRECTION_FRAME_DEFS = [
    {
        "axis_id": "surface_experience_vs_system_logic",
        "label": "表层体验与系统机制是否错位",
        "content_type": "机制解释/行业背景故事",
        "core_question": "围绕{obj}，普通人以为的问题和真正起作用的系统机制是否一致？",
        "need_keywords": [["reception", "comment"], ["decision", "viewpoint", "fact"]],
        "missing_rules": [
            ("缺少普通人真实体验材料，无法证明表层痛点是什么。", ["reception", "comment"]),
            ("缺少机制解释材料，无法把痛点推进到系统层。", ["decision", "viewpoint", "fact"]),
        ],
        "risk": ["容易只替观众出气，最后没有把机制讲明白。"],
        "foundation_items": ["stable_007", "stable_018", "stable_020"],
        "target_source_types": ["comments", "professional_media", "fact"],
        "collector_gap_keywords": ["评论", "接受度", "机制", "原因"],
    },
    {
        "axis_id": "stakeholder_interest_chain",
        "label": "利益链条是否能讲清楚",
        "content_type": "行业机制/背景故事",
        "core_question": "{obj}背后到底有哪些参与方，各自为什么会把问题推到今天这一步？",
        "need_keywords": [["decision", "viewpoint"], ["fact"]],
        "missing_rules": [
            ("缺少利益相关方材料，无法讲清楚谁在链条里起作用。", ["decision", "viewpoint"]),
            ("缺少可核对事实，容易写成阴谋论或情绪控诉。", ["fact"]),
        ],
        "risk": ["容易把复杂行业问题写成单一坏人叙事。"],
        "foundation_items": ["stable_011", "stable_018", "stable_019"],
        "target_source_types": ["professional_media", "authority_database", "case"],
        "collector_gap_keywords": ["利益", "链条", "平台", "黄牛"],
    },
    {
        "axis_id": "rule_gap_or_policy_failure",
        "label": "规则为什么没有解决问题",
        "content_type": "政策/规则解释",
        "core_question": "围绕{obj}，已有规则看起来在管，为什么现实体验仍然没有明显改善？",
        "need_keywords": [["fact", "decision"], ["viewpoint"]],
        "missing_rules": [
            ("缺少规则或治理事实，无法判断问题卡在哪里。", ["fact", "decision"]),
            ("缺少解释性观点，无法说明规则和现实之间的落差。", ["viewpoint"]),
        ],
        "risk": ["容易变成政策口号，缺少具体卡点。"],
        "foundation_items": ["stable_018", "stable_019", "stable_020"],
        "target_source_types": ["official", "professional_media", "case"],
        "collector_gap_keywords": ["规则", "实名制", "监管", "政策"],
    },
    {
        "axis_id": "public_emotion_to_evidence",
        "label": "观众委屈能否被证据承接",
        "content_type": "情绪入口/深度解释",
        "core_question": "{obj}能不能从普通人的委屈出发，但最后落到可验证的事实和解释上？",
        "need_keywords": [["reception", "comment"], ["fact", "decision", "viewpoint"]],
        "missing_rules": [
            ("缺少受众情绪材料，开头容易没有共鸣。", ["reception", "comment"]),
            ("缺少证据承接，情绪容易悬空。", ["fact", "decision", "viewpoint"]),
        ],
        "risk": ["容易只写爽文，不够可信；或只堆资料，情绪入口消失。"],
        "foundation_items": ["stable_007", "stable_019", "stable_020"],
        "target_source_types": ["comments", "fact", "professional_media"],
        "collector_gap_keywords": ["评论", "体验", "情绪"],
    },
    {
        "axis_id": "practical_exit_or_user_choice",
        "label": "普通人最后能怎么选择",
        "content_type": "解释+行动出口",
        "core_question": "讲完{obj}之后，普通观众能得到什么更清醒的判断或选择？",
        "need_keywords": [["decision", "viewpoint"], ["reception", "comment"]],
        "missing_rules": [
            ("缺少行动建议或风险判断材料，结尾容易只剩情绪。", ["decision", "viewpoint"]),
            ("缺少用户场景材料，建议容易脱离真实体验。", ["reception", "comment"]),
        ],
        "risk": ["容易从深度解释突然滑到生活小贴士，内容重心变浅。"],
        "foundation_items": ["stable_018", "stable_020"],
        "target_source_types": ["professional_media", "comments", "case"],
        "collector_gap_keywords": ["建议", "风险", "评论", "体验"],
    },
]


def frames_for_pack(pack: dict[str, Any]) -> list[dict[str, Any]]:
    object_type = str(pack.get("object_type") or "")
    if object_type in {"topic_direction", "event", "abstract_topic", "product_or_org", "mixed_or_unknown"}:
        return TOPIC_DIRECTION_FRAME_DEFS
    return FRAME_DEFS


def all_frames() -> list[dict[str, Any]]:
    return FRAME_DEFS + TOPIC_DIRECTION_FRAME_DEFS


def supplemental_search_queries(task_object: str, missing_decision: str, labels: list[str]) -> list[str]:
    if any(keyword in missing_decision for keyword in ["规则", "监管", "实名", "政策"]):
        return [
            f"{task_object} 实名制 黄牛 监管 为什么无效",
            f"{task_object} 票务平台 黄牛 主办方 监管 分析",
            f"{task_object} 退票 转赠 实名制 票务规则",
        ]
    if any(keyword in missing_decision for keyword in ["利益", "链条", "平台", "主办方", "黄牛"]):
        return [
            f"{task_object} 黄牛 主办方 平台 利益链",
            f"{task_object} 票务 分配 内部票 黄牛",
            f"{task_object} 演出市场 票务平台 分析",
        ]
    if any(keyword in missing_decision for keyword in ["评论", "接受度", "情绪", "体验"]):
        return [
            f"{task_object} 抢票 观众 体验 评论",
            f"{task_object} 乐迷 抢票 集体作战",
            f"{task_object} 社交平台 抢票难 讨论",
        ]
    return [
        f"{task_object} {missing_decision}",
        f"{task_object} {' '.join(labels[:2])}",
        f"{task_object} 原因 机制 分析 案例",
    ]


def material_for_frame(pack: dict[str, Any], frame: dict[str, Any]) -> tuple[list[dict[str, Any]], list[str]]:
    selected: list[dict[str, Any]] = []
    missing: list[str] = []
    for message, keywords in frame["missing_rules"]:
        matches = tasks_by_keywords(pack, keywords)
        count = sum(int(task.get("ok_sources_count") or 0) for task in matches)
        if count < 2:
            missing.append(message)
        selected.extend(matches)
    for gap in domain_collector_gaps(selected, frame.get("collector_gap_keywords", [])):
        if gap not in missing:
            missing.append(gap)
    deduped: list[dict[str, Any]] = []
    seen: set[str] = set()
    for task in selected:
        tid = str(task.get("task_id"))
        if tid and tid not in seen:
            deduped.append(task)
            seen.add(tid)
    return deduped, missing


def shared_targeted_request(
    task_object: str,
    request_id: str,
    missing_decision: str,
    topic_refs: list[dict[str, str]],
    frames: list[dict[str, Any]],
) -> dict[str, Any]:
    labels = [str(frame.get("label")) for frame in frames if frame.get("label")]
    source_types = sorted(
        {
            str(source_type)
            for frame in frames
            for source_type in frame.get("target_source_types", ["official", "professional_media", "comments"])
        }
    )
    return {
        "request_id": request_id,
        "topic_id": topic_refs[0]["topic_id"],
        "topic_title": topic_refs[0]["topic_title"],
        "related_topic_ids": [ref["topic_id"] for ref in topic_refs],
        "related_topic_titles": [ref["topic_title"] for ref in topic_refs],
        "task_object": task_object,
        "triggered_by": "topic-planner",
        "missing_decision": missing_decision,
        "target_questions": [
            missing_decision,
            "这些补充材料分别能支持或削弱哪些候选方向？",
            "同一个材料是否能同时回填多个候选方向，还是只能服务其中一个？",
        ],
        "source_types": source_types,
        "search_queries": supplemental_search_queries(task_object, missing_decision, labels),
        "seed_urls": [],
        "collection_limit": {
            "max_search_results_per_query": 4,
            "max_pages": 10,
            "max_chars_per_page": 50000,
            "blocked_domains": ["instagram.com", "facebook.com", "tiktok.com", "pinterest.com"],
        },
        "expected_output": ["evidence_by_question", "sources", "remaining_gaps", "related_topic_ids"],
    }


def build_shared_requests(task_object: str, topics: list[dict[str, Any]]) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    groups: dict[str, dict[str, Any]] = {}
    for topic in topics:
        frame = next(
            (item for item in all_frames() if item["label"] == topic["topic_development_brief"]["candidate_axis"]),
            all_frames()[0],
        )
        topic_request_ids: list[str] = []
        for missing in topic.get("missing_evidence", []) or []:
            key = clean_text(missing, 160)
            group = groups.setdefault(
                key,
                {
                    "missing_decision": missing,
                    "topic_refs": [],
                    "frames": [],
                },
            )
            ref = {"topic_id": topic["topic_id"], "topic_title": topic["topic_title"]}
            if ref not in group["topic_refs"]:
                group["topic_refs"].append(ref)
            if frame not in group["frames"]:
                group["frames"].append(frame)
        topic["targeted_research_request_ids"] = topic_request_ids

    requests: list[dict[str, Any]] = []
    open_requests: list[dict[str, Any]] = []
    for index, group in enumerate(groups.values(), start=1):
        request_id = f"trr_shared_{index:03d}"
        req = shared_targeted_request(
            task_object,
            request_id,
            group["missing_decision"],
            group["topic_refs"],
            group["frames"],
        )
        requests.append(req)
        open_requests.append(
            {
                "request_id": request_id,
                "topic_id": group["topic_refs"][0]["topic_id"],
                "topic_ids": [ref["topic_id"] for ref in group["topic_refs"]],
                "missing_decision": group["missing_decision"],
            }
        )
        for ref in group["topic_refs"]:
            topic = next((item for item in topics if item["topic_id"] == ref["topic_id"]), None)
            if topic is not None:
                topic.setdefault("targeted_research_request_ids", []).append(request_id)

    for topic in topics:
        ids = topic.get("targeted_research_request_ids", [])
        topic["targeted_research_request_id"] = ids[0] if ids else None
    return requests, open_requests


def build_topics(pack: dict[str, Any], target_count: int) -> list[dict[str, Any]]:
    obj = str(pack.get("task_object") or "研究对象")
    topics: list[dict[str, Any]] = []
    for index, frame in enumerate(frames_for_pack(pack), start=1):
        selected_tasks, missing = material_for_frame(pack, frame)
        evidence = snippets_from_tasks(selected_tasks, 5)
        support_count = sum(int(task.get("ok_sources_count") or 0) for task in selected_tasks)
        values = score(
            material=1 + min(4, support_count // 5),
            depth=4 if "转折" in frame["label"] or "核心" in frame["label"] else 3,
            risk=3 if missing else 4,
            freshness=3,
            user_fit=4,
        )
        topic_id = f"topic_{index:03d}"
        title = f"{obj}：{frame['label']}"
        topics.append(
            {
                "topic_id": topic_id,
                "topic_title": title,
                "content_type": frame["content_type"],
                "core_question": frame["core_question"].format(obj=obj),
                "status": status_from_score(values, missing),
                "why_it_may_work": [
                    "当前资料已经能形成一个可讨论的判断方向。",
                    "但该方向仍需由 agent 或用户基于证据做最终选题表达，不应直接当成成稿标题。",
                ],
                "material_support": evidence,
                "evidence_used": evidence,
                "source_task_ids": task_ids(selected_tasks),
                "missing_evidence": missing,
                "risk": frame["risk"],
                "score": values,
                "recommended_foundation_items": frame.get("foundation_items", []),
                "topic_development_brief": {
                    "candidate_axis": frame["label"],
                    "not_final_title": True,
                    "agent_judgment_required": [
                        "判断这个方向是否真的有新信息，而不是换个说法重述资料。",
                        "判断证据能否支撑一个明确观点，而不是只支撑人物介绍。",
                        "必要时把标题改写成更具体的选题句，但不得脱离 source_task_ids 中的材料。",
                    ],
                    "evidence_profile": {
                        "supporting_task_ids": task_ids(selected_tasks),
                        "support_source_count": support_count,
                        "snippet_count": len(evidence),
                    },
                },
                "targeted_research_request_id": f"trr_{topic_id}_001" if missing else None,
            }
        )
    return topics[:target_count]


def rank_topics(topics: list[dict[str, Any]]) -> list[str]:
    return [
        topic["topic_id"]
        for topic in sorted(
            topics,
            key=lambda item: (
                item["status"] == "strong",
                item["score"]["material_support"] + item["score"]["depth"] + item["score"]["risk_control"],
                -len(item.get("missing_evidence") or []),
            ),
            reverse=True,
        )
    ]


def plan_topics(pack: dict[str, Any], target_count: int) -> dict[str, Any]:
    topics = build_topics(pack, target_count)
    ranking = rank_topics(topics)
    targeted_requests, open_requests = build_shared_requests(str(pack.get("task_object") or ""), topics)

    state_id = f"topic_state_{datetime.now().strftime('%Y%m%d_%H%M%S')}_v1"
    recommended_id = ranking[0] if ranking else None
    recommended_topic = next((topic["topic_title"] for topic in topics if topic["topic_id"] == recommended_id), None)
    return {
        "mode": "initial_plan",
        "planner_mode": "evidence_scaffold",
        "agent_judgment_required": True,
        "human_review_required": True,
        "task_object": pack.get("task_object"),
        "object_type": pack.get("object_type"),
        "domain": pack.get("domain"),
        "collection_plan_id": pack.get("collection_plan_id"),
        "foundation_status": pack.get("foundation_status", {}),
        "foundation_use": {
            "selected_cards_or_rules": sorted({item for topic in topics for item in topic.get("recommended_foundation_items", [])}),
            "selection_reason": ["按候选方向匹配领域判断卡；脚本只给推荐，不直接套用。"],
        },
        "topic_material_profile": build_material_profile(pack),
        "topics": topics,
        "topic_state": {
            "state_id": state_id,
            "task_object": pack.get("task_object"),
            "domain": pack.get("domain"),
            "version": 1,
            "planner_mode": "evidence_scaffold",
            "topics": topics,
            "ranking": ranking,
            "open_requests": open_requests,
        },
        "recommended_topic": recommended_topic,
        "targeted_research_requests": targeted_requests,
        "targeted_research_request_policy": {
            "mode": "shared_by_missing_decision",
            "rule": "同一个 missing_decision 只生成一个补采请求，并通过 related_topic_ids 回填多个候选方向。",
        },
        "next_allowed_steps": [
            "把候选方向交给用户审核或由 agent 基于证据做选题判断",
            "如存在 targeted_research_requests，先交给 research-collector 补采",
            "用户确认某个选题后，才可进入 outline-planner",
        ],
    }


def parse_args(argv: list[str]) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--research-pack", required=True, type=Path)
    parser.add_argument("--out", required=True, type=Path)
    parser.add_argument("--target-count", type=int, default=5)
    return parser.parse_args(argv)


def main(argv: list[str]) -> int:
    args = parse_args(argv)
    write_json(args.out, plan_topics(load_json(args.research_pack), args.target_count))
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
