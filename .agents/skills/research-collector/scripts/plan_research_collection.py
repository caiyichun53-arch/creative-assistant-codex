#!/usr/bin/env python3
"""Create a deterministic collection_plan for a research request.

The planner turns a broad input such as a person, topic, theme, or direction
into concrete collection tasks. It does not fetch web pages and does not write
topic plans or prose.
"""

from __future__ import annotations

import argparse
import json
import re
import sys
from datetime import datetime
from pathlib import Path
from typing import Any


REPO_ROOT = Path(__file__).resolve().parents[4]
SKILLS_ROOT = REPO_ROOT / ".agents" / "skills"
DOMAIN_REGISTRY = SKILLS_ROOT / "domain-foundation-registry.json"


DEPTH_OPTIONS = {
    "quick": {
        "label": "快速研究",
        "best_for": "只需要建立基本认知或粗略判断对象是否值得继续。",
        "tradeoff": "速度快，但观点层和接受度层较浅。",
        "limits": {"max_search_results_per_query": 3, "max_pages": 6, "max_chars_per_page": 30000},
    },
    "standard": {
        "label": "标准研究",
        "best_for": "支撑一次选题规划和基础大纲判断。",
        "tradeoff": "覆盖事实、观点、接受度三层，但不会做大规模评论采样。",
        "limits": {"max_search_results_per_query": 4, "max_pages": 12, "max_chars_per_page": 50000},
    },
    "deep": {
        "label": "深度研究",
        "best_for": "支撑深度视频、长稿、人物传记或争议型选题。",
        "tradeoff": "耗时更长，采集范围更大，需要更严格审查。",
        "limits": {"max_search_results_per_query": 6, "max_pages": 24, "max_chars_per_page": 70000},
    },
}

OBJECT_PATTERNS = [
    ("topic_direction", re.compile(r"(为什么|如何|怎么|背后|困境|趋势|误读|复出|走红|衰落|转型|盘点|分析)")),
    ("event", re.compile(r"(事件|发布会|事故|政策|比赛|争议|战争|选举|大会|演唱会)")),
    ("work", re.compile(r"(电影|电视剧|纪录片|专辑|歌曲|书|小说|论文|游戏|节目|作品|MV)")),
    ("product_or_org", re.compile(r"(公司|品牌|产品|工具|平台|项目|城市|学校|机构)")),
    ("abstract_topic", re.compile(r"(主义|经济|技术|概念|文化|心理|制度|算法|检测|模型|教育|医疗|金融)")),
]

KNOWN_OBJECT_TYPES = {
    "person",
    "person_or_entity",
    "work",
    "song",
    "album",
    "product_or_org",
    "event",
    "topic_direction",
    "abstract_topic",
    "mixed_or_unknown",
}

MUSIC_TERMS = re.compile(
    r"(音乐|歌手|乐队|专辑|歌曲|唱片|演唱会|作曲|作词|编曲|R&B|Soul|流行乐|娱乐人物|艺人|演员|偶像|传记|方大同|徐怀钰|周杰伦|王菲|陈奕迅)",
    re.I,
)
NON_MUSIC_ENTERTAINMENT_TOPIC_TERMS = re.compile(
    r"(演唱会).*(门票|抢票|票务|黄牛|主办方|平台|监管|实名制|退票|转赠)|"
    r"(门票|抢票|票务|黄牛|主办方|平台|监管|实名制|退票|转赠).*(演唱会)",
    re.I,
)


def load_json(path: Path) -> dict[str, Any]:
    return json.loads(path.read_text(encoding="utf-8-sig"))


def write_json(path: Path, data: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")


def infer_object_type(text: str) -> str:
    for object_type, pattern in OBJECT_PATTERNS:
        if pattern.search(text):
            return object_type
    if len(text.strip()) <= 12 and not re.search(r"[，。！？；:：]", text):
        return "person_or_entity"
    return "mixed_or_unknown"


def normalize_object_type(value: Any) -> str | None:
    if not value:
        return None
    object_type = str(value).strip()
    return object_type if object_type in KNOWN_OBJECT_TYPES else None


def infer_domain(text: str, explicit_domain: str | None) -> tuple[str, str]:
    if explicit_domain and explicit_domain != "unknown":
        return explicit_domain, "explicit_input"
    if NON_MUSIC_ENTERTAINMENT_TOPIC_TERMS.search(text):
        return "unknown", "ticketing_mechanism_not_music_foundation"
    if MUSIC_TERMS.search(text):
        return "music_entertainment", "keyword_match"
    return "unknown", "no_domain_match"


def source_domain_from_seed(request: dict[str, Any]) -> tuple[str | None, str | None]:
    constraints = request.get("source_constraints") or {}
    seed_source = constraints.get("seed_source")
    if not seed_source:
        return None, None
    path = Path(str(seed_source))
    if not path.exists():
        return None, str(seed_source)
    try:
        text = path.read_text(encoding="utf-8-sig")
    except Exception:
        return None, str(seed_source)
    if not text.startswith("---"):
        return None, str(seed_source)
    match = re.search(r"^domain:\s*['\"]?([^'\"\n]+)['\"]?\s*$", text, flags=re.M)
    if not match:
        return None, str(seed_source)
    return match.group(1).strip(), str(seed_source)


def depth_limits(depth: str) -> dict[str, int]:
    return dict(DEPTH_OPTIONS.get(depth, DEPTH_OPTIONS["standard"])["limits"])


def normalize_depth(value: Any) -> tuple[str, bool]:
    if value in DEPTH_OPTIONS:
        return str(value), True
    return "standard", False


def review_mode(request: dict[str, Any], domain: str) -> dict[str, Any]:
    maturity = str(request.get("maturity") or "experimental")
    autonomy_mode = str(request.get("autonomy_mode") or "review_first")
    can_run_without_human_approval = autonomy_mode == "auto" and maturity == "stable"
    reasons = []
    if not can_run_without_human_approval:
        reasons.append("默认 review_first：当前流程仍在打磨阶段，采集计划必须先给用户审核。")
    if domain == "unknown":
        reasons.append("领域未知：需要用户确认是否启用某个领域采集方向。")
    return {
        "maturity": maturity,
        "autonomy_mode": autonomy_mode,
        "requires_human_review": not can_run_without_human_approval,
        "can_run_without_human_approval": can_run_without_human_approval,
        "review_required_before": ["collect_research_pack"],
        "review_items": [
            "object_type 是否判断正确",
            "domain 是否判断正确",
            "research_depth 是否合适",
            "collection_tasks 是否过多、过少或偏题",
            "domain_specific_tasks 是否应该启用",
            "blocked_domains / allowed_domains 是否符合限制",
        ],
        "reasons": reasons,
    }


def search_query(task_object: str, suffix: str) -> str:
    return f"{task_object} {suffix}".strip()


def load_domain_registry() -> dict[str, Any]:
    if not DOMAIN_REGISTRY.exists():
        return {}
    try:
        return load_json(DOMAIN_REGISTRY)
    except Exception:
        return {}


def domain_skill_path(domain: str) -> Path | None:
    registry = load_domain_registry()
    entry = (registry.get("domains") or {}).get(domain)
    if entry and entry.get("path"):
        path = REPO_ROOT / str(entry["path"])
        if path.exists():
            return path
    fallback = SKILLS_ROOT / f"{domain}-foundation"
    return fallback if fallback.exists() else None


def load_collection_map(domain: str) -> dict[str, Any] | None:
    path = domain_skill_path(domain)
    if not path:
        return None
    map_path = path / "references" / "collection_map.json"
    if not map_path.exists():
        return None
    return load_json(map_path)


def map_object_type(collection_map: dict[str, Any], object_type: str) -> str:
    aliases = collection_map.get("object_type_aliases") or {}
    mapped = aliases.get(object_type, object_type)
    tasks = collection_map.get("tasks") or {}
    if mapped in tasks:
        return mapped
    if object_type in tasks:
        return object_type
    if object_type in {"event", "abstract_topic", "mixed_or_unknown", "product_or_org"} and "topic_direction" in tasks:
        return "topic_direction"
    if "topic_direction" in tasks:
        return "topic_direction"
    if object_type in {"person", "person_or_entity"} and "person" in tasks:
        return "person"
    return next(iter(tasks.keys()), object_type)


def instantiate_domain_task(task_object: str, template: dict[str, Any]) -> dict[str, Any]:
    task = {k: v for k, v in template.items() if k != "search_query_templates"}
    task["search_queries"] = [
        str(q).replace("{task_object}", task_object)
        for q in template.get("search_query_templates", [])
    ]
    task.setdefault("seed_urls", [])
    return task


def base_tasks(task_object: str, object_type: str, depth: str) -> list[dict[str, Any]]:
    tasks: list[dict[str, Any]] = [
        {
            "task_id": "fact_001",
            "layer": "fact",
            "purpose": "确认基础定义、身份、时间线和关键事实",
            "source_priority": ["official", "authority_database", "mainstream_media"],
            "target_questions": [
                "这个对象的基础定义、身份或背景是什么？",
                "有哪些必须核对的关键时间线和事实？",
                "哪些事实存在版本冲突或需要谨慎表述？",
            ],
            "search_queries": [
                search_query(task_object, "官方 简介 时间线"),
                search_query(task_object, "采访 报道 关键事实"),
            ],
            "seed_urls": [],
        },
        {
            "task_id": "viewpoint_001",
            "layer": "viewpoint",
            "purpose": "收集专业观点、媒体评论和主要解释框架",
            "source_priority": ["professional_media", "review", "interview"],
            "target_questions": [
                "专业或媒体材料通常如何评价这个对象？",
                "有哪些反复出现的解释框架、争议或误读？",
            ],
            "search_queries": [
                search_query(task_object, "评论 分析 采访"),
                search_query(task_object, "争议 误读 深度报道"),
            ],
            "seed_urls": [],
        },
        {
            "task_id": "reception_001",
            "layer": "reception",
            "purpose": "收集公众接受度、用户反馈、传播入口和情绪材料",
            "source_priority": ["community", "comments", "social_or_platform_reception"],
            "target_questions": [
                "普通用户或受众最常从哪里记住它？",
                "公众反馈里反复出现的情绪、标签或争议是什么？",
            ],
            "search_queries": [
                search_query(task_object, "评论 评价 热门"),
                search_query(task_object, "豆瓣 短评 长评"),
            ],
            "seed_urls": [],
        },
    ]

    if object_type == "topic_direction":
        tasks.append(
            {
                "task_id": "decision_001",
                "layer": "decision_support",
                "purpose": "验证输入方向是否有足够材料支撑",
                "source_priority": ["fact", "viewpoint", "case"],
                "target_questions": [
                    "这个思路方向的核心判断是否有事实支撑？",
                    "有哪些材料会削弱或推翻这个方向？",
                ],
                "search_queries": [
                    search_query(task_object, "原因 机制 案例 分析"),
                    search_query(task_object, "争议 风险 规则 现实"),
                ],
                "seed_urls": [],
            }
        )
    return tasks


def domain_tasks_from_map(task_object: str, domain: str, object_type: str) -> tuple[list[dict[str, Any]], str | None]:
    collection_map = load_collection_map(domain)
    if not collection_map:
        return [], f"missing collection_map for domain {domain}"
    mapped_type = map_object_type(collection_map, object_type)
    templates = (collection_map.get("tasks") or {}).get(mapped_type, [])
    tasks = [instantiate_domain_task(task_object, template) for template in templates]
    return tasks, None


def apply_source_constraints(tasks: list[dict[str, Any]], request: dict[str, Any]) -> None:
    constraints = request.get("source_constraints") or {}
    user_limits = request.get("user_limits") or []
    blocked = set(constraints.get("blocked_domains") or [])
    allowed = constraints.get("allowed_domains") or []
    if isinstance(user_limits, str):
        user_limits = [user_limits]
    if any("八卦" in str(x) or "娱乐新闻" in str(x) for x in user_limits):
        blocked.update(["weibo.com", "instagram.com", "facebook.com", "tiktok.com"])
    for task in tasks:
        task.setdefault("collection_limit", {})
        if blocked:
            task["collection_limit"]["blocked_domains"] = sorted(blocked)
        if allowed:
            task["collection_limit"]["allowed_domains"] = allowed


def estimate_collection_budget(tasks: list[dict[str, Any]]) -> dict[str, Any]:
    estimated_queries = 0
    estimated_pages = 0
    max_chars_total = 0
    for task in tasks:
        queries = len(task.get("search_queries") or [])
        seed_urls = len(task.get("seed_urls") or [])
        limits = task.get("collection_limit") or {}
        max_results = int(limits.get("max_search_results_per_query") or 0)
        max_pages = int(limits.get("max_pages") or 0)
        max_chars = int(limits.get("max_chars_per_page") or 0)
        task_page_cap = min(max_pages, seed_urls + queries * max_results) if max_pages else seed_urls + queries * max_results
        estimated_queries += queries
        estimated_pages += task_page_cap
        max_chars_total += task_page_cap * max_chars
    high_cost = estimated_queries > 8 or estimated_pages > 20 or len(tasks) > 4
    return {
        "estimated_tasks": len(tasks),
        "estimated_search_queries": estimated_queries,
        "estimated_max_pages": estimated_pages,
        "estimated_max_chars": max_chars_total,
        "cost_level": "high" if high_cost else "normal",
        "requires_user_confirmation": True,
        "status": "pending_user_review",
        "approval_note": "采集前必须把预算预估展示给用户；用户确认后才能批准计划。",
        "thresholds": {
            "high_cost_if_search_queries_gt": 8,
            "high_cost_if_max_pages_gt": 20,
            "high_cost_if_tasks_gt": 4,
        },
    }


def build_plan(request: dict[str, Any]) -> dict[str, Any]:
    task_object = str(request.get("task_object") or request.get("input") or "").strip()
    if not task_object:
        raise SystemExit("request must include task_object or input")
    object_type = normalize_object_type(request.get("object_type")) or infer_object_type(task_object)
    depth, depth_explicit = normalize_depth(request.get("research_depth"))
    source_domain, seed_source = source_domain_from_seed(request)
    explicit_domain = request.get("domain")
    domain_conflict = None
    if source_domain and (not explicit_domain or explicit_domain == "unknown"):
        domain, domain_reason = source_domain, "seed_source_frontmatter"
    else:
        domain, domain_reason = infer_domain(task_object, explicit_domain)
    if source_domain and explicit_domain and explicit_domain != "unknown" and str(explicit_domain) != source_domain:
        domain_conflict = {
            "explicit_domain": str(explicit_domain),
            "source_domain": source_domain,
            "seed_source": seed_source,
            "resolution_required": "请求 domain 与来源爆款 frontmatter 不一致，必须人工确认后重新生成采集计划。",
        }
    tasks = base_tasks(task_object, object_type, depth)
    domain_specific: list[dict[str, Any]] = []
    domain_collection_gap = None
    if domain != "unknown" and not domain_conflict:
        domain_specific, domain_collection_gap = domain_tasks_from_map(task_object, domain, object_type)
        tasks.extend(domain_specific)
    elif domain_conflict:
        domain_collection_gap = "domain_conflict: domain-specific collection disabled until user resolves domain"

    limits = depth_limits(depth)
    for task in tasks:
        merged_limits = dict(limits)
        merged_limits.update(task.get("collection_limit") or {})
        task["collection_limit"] = merged_limits

    apply_source_constraints(tasks, request)
    budget_review = estimate_collection_budget(tasks)

    plan_id = request.get("plan_id") or f"collection_plan_{datetime.now().strftime('%Y%m%d_%H%M%S')}"
    gate = review_mode(request, domain)
    approval_status = "auto_approved" if gate["can_run_without_human_approval"] else "pending_user_review"
    return {
        "plan_id": plan_id,
        "mode": "analyze_collection_need",
        "task_object": task_object,
        "object_type": object_type,
        "domain": domain,
        "domain_reason": domain_reason,
        "source_domain": source_domain,
        "domain_conflict": domain_conflict,
        "research_depth": depth,
        "research_depth_decision": {
            "selected": depth,
            "explicitly_requested": depth_explicit,
            "requires_user_confirmation": not depth_explicit and gate["requires_human_review"],
            "options": DEPTH_OPTIONS,
            "selection_note": "未显式指定研究深度时默认 standard，但在 review_first 模式下必须给用户审核确认。",
        },
        "review_gate": gate,
        "budget_review": budget_review,
        "approval": {
            "status": approval_status,
            "approved_by": "script" if approval_status == "auto_approved" else None,
            "approved_at": datetime.now().isoformat(timespec="seconds") if approval_status == "auto_approved" else None,
            "approval_note": "auto mode with stable maturity" if approval_status == "auto_approved" else "waiting for user review",
        },
        "foundation_status": {
            "loaded": domain != "unknown",
            "domain": domain,
            "foundation_gap": domain_collection_gap if domain != "unknown" else "no matching domain foundation",
        },
        "collection_strategy": {
            "fact_layer": [t["task_id"] for t in tasks if "fact" in t["layer"]],
            "viewpoint_layer": [t["task_id"] for t in tasks if "viewpoint" in t["layer"]],
            "reception_layer": [t["task_id"] for t in tasks if "reception" in t["layer"]],
            "domain_specific_tasks": [t["task_id"] for t in domain_specific],
        },
        "collection_tasks": tasks,
        "expected_research_pack_fields": [
            "summary",
            "timeline_or_evolution",
            "key_facts",
            "representative_materials",
            "main_viewpoints",
            "common_misreadings",
            "usable_materials",
            "background_only_materials",
            "high_risk_materials",
            "audience_or_public_reception",
            "sources",
            "research_gaps",
            "compliance_report",
        ],
        "forbidden_outputs": ["topics", "outline", "draft"],
    }


def parse_args(argv: list[str]) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--request", required=True, type=Path)
    parser.add_argument("--out", required=True, type=Path)
    return parser.parse_args(argv)


def main(argv: list[str]) -> int:
    args = parse_args(argv)
    plan = build_plan(load_json(args.request))
    write_json(args.out, plan)
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
