"""生成总编辑任务合同:材料包 + 方法论路由 + 少量范例 → creation_contract.md。

这是创作编排的确定性闸口。总编辑先把经验转译为本次任务合同,
写手不得绕过合同临场发挥;审稿也必须按合同验收。
"""
from __future__ import annotations

import argparse
import re
import sys
from dataclasses import dataclass
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
VAULT = ROOT / "vault"
TEMPLATE = ROOT / "templates" / "写手_dispatch.md"


@dataclass
class Route:
    play: str
    condition: str
    tags: str
    hits: list[int]
    score: int = 0


@dataclass
class Example:
    path: Path
    bucket: str
    play: str
    strength: int
    source_id: int | None
    title: str
    body: str
    score: int = 0


KEYWORDS: dict[str, list[str]] = {
    "品牌/产品传奇弧·商业逻辑·当下启示": [
        "品牌", "产品", "公司", "市场", "本土", "外资", "可口可乐", "Thums Up", "Campa", "汽水",
        "饮料", "买下", "复活", "传奇", "历史",
    ],
    "价格悬疑·成本拆解·消费者指南": [
        "价格", "成本", "便宜", "贵", "十卢比", "卢比", "包装", "装瓶", "渠道", "价格带",
    ],
    "系统性内幕·案例实锤·觉醒工具包": [
        "行业", "规则", "潜规则", "争议", "双重标准", "污染", "农药", "抵制",
    ],
    "数字对撞钩": [
        "14亿", "42%", "10 亿", "6000 万", "9 瓶", "十卢比", "第一", "全球", "市场份额",
    ],
    "认知颠覆钩": [
        "反向", "误导", "不是", "其实", "真正", "反转", "信息差", "悖论",
    ],
    "亲历颠覆钩": [
        "街头", "小店", "消费者", "你", "刷到", "干净又卫生", "饮料柜",
    ],
    "阵营施压钩": [
        "懂", "不懂", "美国", "印度", "本土", "外资", "标准答案",
    ],
    "降维重命名": [
        "不是", "而是", "身份", "仪式", "文化", "本土", "印度脸", "标准答案",
    ],
    "裁判终判句": [
        "市场", "时代", "规则", "淘汰", "真正", "不是", "而是",
    ],
}


def read_text(path: Path) -> str:
    return path.read_text(encoding="utf-8")


def compact(s: str) -> str:
    return re.sub(r"\s+", " ", s).strip()


def parse_simple_yaml(path: Path) -> dict[str, str]:
    data: dict[str, str] = {}
    if not path.exists():
        return data
    for line in read_text(path).splitlines():
        if not line or line.lstrip().startswith("#") or ":" not in line:
            continue
        key, value = line.split(":", 1)
        data[key.strip()] = value.split("#", 1)[0].strip()
    return data


def parse_routes(path: Path) -> list[Route]:
    routes: list[Route] = []
    for line in read_text(path).splitlines():
        line = line.strip()
        if not line.startswith("|") or line.startswith("|---") or "打法" in line and "触发条件" in line:
            continue
        cells = [c.strip() for c in line.strip("|").split("|")]
        if len(cells) < 4:
            continue
        hits = [int(n) for n in re.findall(r"\d+", cells[3])]
        routes.append(Route(cells[0], cells[1], cells[2], hits))
    return routes


def frontmatter_value(text: str, key: str, default: str = "") -> str:
    match = re.search(rf"^{re.escape(key)}:\s*(.+)$", text, re.M)
    if not match:
        return default
    return match.group(1).strip().strip('"')


def parse_example(path: Path, bucket: str) -> Example:
    text = read_text(path)
    title = ""
    for line in text.splitlines():
        if line.startswith("# "):
            title = line[2:].strip()
            break
    source_raw = frontmatter_value(text, "source_id")
    return Example(
        path=path,
        bucket=bucket,
        play=frontmatter_value(text, "play"),
        strength=int(frontmatter_value(text, "strength", "0") or 0),
        source_id=int(source_raw) if source_raw.isdigit() else None,
        title=title or path.stem,
        body=text,
    )


def score_text(play: str, haystack: str) -> int:
    score = 0
    for kw in KEYWORDS.get(play, []):
        if kw and kw in haystack:
            score += 3
    for token in re.findall(r"[\u4e00-\u9fffA-Za-z0-9]+", play):
        if len(token) >= 2 and token in haystack:
            score += 1
    return score


def select_routes(routes: list[Route], brief: str, limit: int) -> list[Route]:
    for route in routes:
        route.score = score_text(route.play, brief) + score_text(route.condition, brief)
        for hit in route.hits:
            if f"hit {hit}" in brief or f"hit_id: {hit}" in brief:
                route.score += 4
    return sorted(routes, key=lambda r: (r.score, len(r.hits)), reverse=True)[:limit]


def load_examples(bucket: str) -> list[Example]:
    folder = VAULT / "范例" / bucket
    if not folder.exists():
        return []
    return [parse_example(p, bucket) for p in folder.glob("*.md") if p.name != ".gitkeep"]


def select_examples(bucket: str, routes: list[Route], brief: str, limit: int) -> list[Example]:
    route_plays = {r.play for r in routes}
    route_hits = {hit for r in routes for hit in r.hits}
    examples = load_examples(bucket)
    for ex in examples:
        ex.score = ex.strength * 5
        if ex.play in route_plays:
            ex.score += 30
        if ex.source_id in route_hits:
            ex.score += 12
        ex.score += score_text(ex.play, brief)
        ex.score += min(8, score_text(ex.title, brief))
        if "可乐" in brief and any(word in ex.body for word in ("饮料", "汽水", "可乐")):
            ex.score += 10
    return sorted(examples, key=lambda e: (e.score, e.strength), reverse=True)[:limit]


def format_routes(title: str, routes: list[Route]) -> str:
    lines: list[str] = []
    for i, route in enumerate(routes, 1):
        lines.append(
            f"{i}. {route.play}（score={route.score}）\n"
            f"   - 触发条件:{route.condition}\n"
            f"   - 标签:{route.tags}\n"
            f"   - 代表 hit:{', '.join(map(str, route.hits)) or '无'}"
        )
    return "\n".join(lines) if lines else "（无匹配）"


def excerpt_example(ex: Example) -> str:
    keep: list[str] = []
    capture = False
    for line in ex.body.splitlines():
        if line.startswith("## 范例本体") or line.startswith("## 为什么有效") or line.startswith("## 怎么照做"):
            capture = True
            keep.append(line)
            continue
        if line.startswith("## ") and capture:
            capture = False
        if capture:
            keep.append(line)
    excerpt = "\n".join(keep).strip()
    if len(excerpt) > 1000:
        excerpt = excerpt[:1000].rstrip() + "\n...(已截断)"
    rel = ex.path.relative_to(ROOT)
    return (
        f"### {ex.bucket}范例 · {ex.title}\n"
        f"- 来源:{rel}\n"
        f"- 打法:{ex.play} | strength={ex.strength} | score={ex.score} | source_id={ex.source_id or '无'}\n\n"
        f"{excerpt or compact(ex.body)[:800]}"
    )


def primary_play(routes: list[Route]) -> str:
    if not routes:
        return "未匹配到主打法,写手必须先停下询问总编辑"
    route = routes[0]
    return f"{route.play}（触发条件:{route.condition}; score={route.score}）"


def route_contract(kind: str, routes: list[Route]) -> str:
    if not routes:
        return f"- {kind}:未匹配到主打法。写手不得自行选择,必须先请总编辑补合同。"
    route = routes[0]
    if kind == "钩子":
        return (
            f"- 主打法:{route.play}\n"
            "- 第一秒只抛冲突、反常识或未解问题,不解释原因。\n"
            "- 第一句禁止说出核心答案;必须把答案留到后文兑现。\n"
            "- 开头必须是能直接说出口的一句话,不要用画面和铺垫。\n"
            f"- 若需要换打法,只能在总编辑确认后从候选里换;当前触发条件:{route.condition}"
        )
    if kind == "结构":
        return (
            f"- 主打法:{route.play}\n"
            "- 大纲必须拆成 3-5 拍,每一拍只承担一个信息动作。\n"
            "- 第一拍承接钩子的未解问题,第二拍开始交代原因或证据。\n"
            "- 每一拍都要比上一拍多一个事实、判断或转折,不能只换说法重复。\n"
            f"- 当前打法触发条件:{route.condition}"
        )
    return (
        f"- 主打法:{route.play}\n"
        "- 成稿优先保持口播感和判断力,不要把方法论术语写进正文。\n"
        "- 用短句压住观点,解释点到即止;信息给完就停。\n"
        f"- 当前打法触发条件:{route.condition}"
    )


def section_excerpt(text: str, heading: str, max_chars: int = 260) -> str:
    pattern = re.compile(
        rf"^##\s+{re.escape(heading)}\s*$([\s\S]*?)(?=^##\s+|\Z)",
        re.M,
    )
    match = pattern.search(text)
    if not match:
        return ""
    body = compact(match.group(1))
    return body[:max_chars].rstrip()


def example_action(ex: Example) -> str:
    how = section_excerpt(ex.body, "怎么照做") or section_excerpt(ex.body, "为什么有效")
    if not how:
        how = compact(ex.body)[:220]
    if ex.bucket == "钩子":
        prefix = "钩子动作"
        action = "第一句照这个动作制造悬念,不要照抄句子"
    elif ex.bucket == "结构":
        prefix = "结构动作"
        action = "大纲照这个动作安排信息顺序,不要复述对标原文"
    else:
        prefix = "文风动作"
        action = "成稿照这个动作处理句子节奏和口语判断"
    return (
        f"- {prefix}:来自 {ex.path.relative_to(ROOT)}\n"
        f"  - 总编辑转译:{action}。\n"
        f"  - 证据摘录:{how or '无可用摘录'}"
    )


def format_experience_actions(examples: list[Example]) -> str:
    if not examples:
        return "- 暂无足够相关范例。写手只能按方法论合同和材料包写,不得编造经验来源。"
    return "\n".join(example_action(ex) for ex in examples)


def review_contract() -> str:
    return (
        "- 钩子验收:第一句若剧透核心答案、空铺垫、靠画面成立,直接 FAIL。\n"
        "- 句子验收:第一句读出来拗口、像报告标题,或用抽象概念当主语且没有翻译成具体动作,直接 FAIL。\n"
        "- 结构验收:大纲或成稿没有兑现结构主打法,或每拍没有新增信息,直接 FAIL。\n"
        "- 增量验收:正文没有用研究包里竞品没讲的信息,直接 FAIL。\n"
        "- 经验验收:经验只出现在【思考】里、正文句子没体现对应动作,直接 FAIL。\n"
        "- 体裁验收:出现分镜/画面/BGM/字幕等非口播内容,直接 FAIL。\n"
        "- 篇幅验收:明显偏离目标范围,至少 WARN,严重偏离打回。"
    )


def fill(template: str, values: dict[str, str]) -> str:
    out = template
    for key, value in values.items():
        out = out.replace("{{" + key + "}}", value)
    return out


def build(topic_id: int) -> Path:
    topic_dir = ROOT / "data" / "topics" / str(topic_id)
    brief_path = topic_dir / "brief.md"
    if not brief_path.exists():
        raise SystemExit(f"材料包不存在: {brief_path}")
    brief = read_text(brief_path)

    account = parse_simple_yaml(ROOT / "config" / "accounts" / "张芝士.yaml")
    domain = parse_simple_yaml(ROOT / "config" / "domains" / "泛科普.yaml")

    structure_routes = select_routes(
        parse_routes(VAULT / "方法论" / "结构打法路由表.md"), brief, 2
    )
    hook_routes = select_routes(
        parse_routes(VAULT / "方法论" / "钩子打法路由表.md"), brief, 2
    )
    style_routes = select_routes(
        parse_routes(VAULT / "方法论" / "文风打法路由表.md"), brief, 1
    )

    examples = (
        select_examples("结构", structure_routes, brief, 1)
        + select_examples("钩子", hook_routes, brief, 1)
        + select_examples("文风", style_routes, brief, 1)
    )

    evidence = (
        "## 经验使用方式(写手【思考】引用这里)\n"
        "- 每个动笔阶段只选 1 条最相关的主经验,说清借的动作;不要把多个范例拼成清单。\n"
        "- 钩子阶段优先看钩子范例;大纲阶段优先看结构范例;成稿阶段优先看文风范例或真人写作基石。\n"
        "- 其它候选只用来排除不合适打法,不得为了证明使用经验而塞进正文。"
    )

    target = domain.get("script_target_chars", "1500")
    range_match = re.search(r"script_chars_range:\s*\[(\d+),\s*(\d+)\]", read_text(ROOT / "config" / "domains" / "泛科普.yaml"))
    low, high = range_match.groups() if range_match else ("1300", "1700")

    contract = fill(
        read_text(TEMPLATE),
        {
            "账号": account.get("name", "张芝士"),
            "选题id": str(topic_id),
            "领域": account.get("domain", domain.get("name", "泛科普")),
            "人设路径": account.get("persona", "vault/人设/张芝士.md"),
            "brief路径": str(brief_path.relative_to(ROOT)),
            "主结构打法": primary_play(structure_routes),
            "主钩子打法": primary_play(hook_routes),
            "主文风打法": primary_play(style_routes),
            "结构打法候选": format_routes("结构打法候选", structure_routes),
            "钩子打法候选": format_routes("钩子打法候选", hook_routes),
            "钩子合同": route_contract("钩子", hook_routes),
            "结构合同": route_contract("结构", structure_routes),
            "文风合同": route_contract("文风", style_routes),
            "经验转译": format_experience_actions(examples),
            "审稿合同": review_contract(),
            "范例": evidence + "\n\n" + "\n\n---\n\n".join(excerpt_example(ex) for ex in examples),
            "目标篇幅": target,
            "篇幅下限": low,
            "篇幅上限": high,
            "草稿路径": str((topic_dir / "draft_v1.md").relative_to(ROOT)),
        },
    )

    out = topic_dir / "creation_contract.md"
    out.write_text(contract, encoding="utf-8", newline="\n")
    # 兼容旧流程入口:内容同合同,避免还没改完的调用方找不到 writer_dispatch.md。
    (topic_dir / "writer_dispatch.md").write_text(contract, encoding="utf-8", newline="\n")
    return out


def main() -> None:
    p = argparse.ArgumentParser()
    p.add_argument("topic_id", type=int)
    args = p.parse_args()
    out = build(args.topic_id)
    print(f"已生成总编辑任务合同: {out.relative_to(ROOT)}")


if __name__ == "__main__":
    try:
        main()
    except BrokenPipeError:
        sys.exit(1)
