"""评论噪音过滤(纯确定性规则,零 LLM —— 禁令/过滤进代码,不依赖模型)。

逆向备料用:对爆款评论先按点赞取 top N,再滤掉噪音(广告导流/灌水/纯表情/复读)。
明天的评论抓取脚本(fetch_comments)抓回原始评论后,统一调 clean_comments() 过滤再入库。

字段约定:评论 dict 至少含 content(文本)、like_count(点赞);其余字段透传保留。
"""
import re

# 广告 / 导流 / 营销(命中即丢)
AD_PAT = re.compile(
    r"(微信|加\s*[vViÌ]|v\s*x|薇信|徽信|威信|➕|＋|加我|私信|私我|工作室|代理|招商|"
    r"兼职|日入|月入|引流|涨粉|互粉|互关|回关|刷单|带货合作|商务合作|接广)",
    re.I,
)
# 纯表情 / 符号 / 数字 / 空白(没有实质文字内容)
EMOJI_ONLY = re.compile(r"^[\W\d\s\U0001F000-\U0001FAFF]+$")

# 每条爆款过滤后保留的评论数(唯一真值:抓取/备料/拆解读取都引这个,改这一处即全局生效)
TOP_COMMENTS = 30


def clean_comments(comments: list[dict], top_n: int = TOP_COMMENTS, min_len: int = 5) -> list[dict]:
    """按点赞降序取 top_n,沿途滤噪音、去复读。返回过滤后的评论(保留原字段)。

    - 超短(< min_len 字)/ 纯表情符号 / 广告导流 → 丢
    - 内容近似复读(去空白后前 30 字相同)→ 只留点赞最高的那条
    """
    ranked = sorted(comments, key=lambda c: c.get("like_count") or 0, reverse=True)
    seen: set[str] = set()
    out: list[dict] = []
    for c in ranked:
        text = (c.get("content") or "").strip()
        if len(text) < min_len:
            continue
        if EMOJI_ONLY.match(text):
            continue
        if AD_PAT.search(text):
            continue
        key = re.sub(r"\s+", "", text)[:30]
        if key in seen:
            continue
        seen.add(key)
        out.append(c)
        if len(out) >= top_n:
            break
    return out


if __name__ == "__main__":
    # 自测样例
    demo = [
        {"content": "讲得太好了,涨知识!", "like_count": 320},
        {"content": "😂😂😂", "like_count": 99},
        {"content": "加微信vx123带你日入过千", "like_count": 5},
        {"content": "讲得太好了,涨知识!", "like_count": 12},   # 复读
        {"content": "顶", "like_count": 8},                    # 超短
        {"content": "这个观点我不同意,冰水其实……", "like_count": 210},
    ]
    for c in clean_comments(demo, top_n=10):
        print(c["like_count"], c["content"])
