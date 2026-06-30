---
name: research-collector
description: 通用研究资料包技能。用于用户给出选题、人物、作品、产品、组织、地点、事件、主题、思路方向、抽象话题或概念后，先用脚本分析采集需求并生成 collection_plan，再按计划脚本化采集 research_pack，最后逐项合规审查。不直接生成选题、不写大纲、不写正文；可按领域加载知识包来注入特定采集方向、资料价值和风险边界。
---

# 通用研究资料包

## 使用时机

当用户给出一个较具体的研究目标，或现有材料不足以支撑后续选题、大纲、写作判断时使用。

`task_object` 通常应是可落地研究的具体输入，例如一个人物、一个作品、一个品牌、一个事件、一个话题，或一个已经带方向的选题雏形。抽象概念也允许，但必须能被界定研究边界。

- 人物：企业家、演员、科学家、运动员、歌手。
- 作品：书、电影、专辑、歌曲、游戏、论文。
- 产品或组织：公司、品牌、工具、城市项目。
- 事件：一次发布会、一次比赛、一次政策变化。
- 抽象话题或概念：注意力经济、低空经济、城市更新、AI 检测、长期主义。
- 选题方向：某人物的复出困境、某产品为什么突然走红、某概念被误读的原因。

## 硬性原则

固定技术路线必须脚本化。不得让 agent 每次临场决定“搜什么、怎么搜、搜多少、用什么渠道”。

本技能必须按以下路线执行：

1. 用脚本生成 `collection_plan`。
2. 用脚本审查 `collection_plan`。
3. 默认停下，把 `collection_plan` 交给用户审核。
4. 用户确认后，用脚本批准 `collection_plan`。
5. 用脚本按批准后的 `collection_plan` 采集 `research_pack`。
6. 用脚本审查 `research_pack`。
7. 只有审查通过后，agent 才能基于证据做摘要整理。

禁止：

- 跳过 `collection_plan` 直接搜索。
- 跳过合规审查直接交给选题、大纲或写作。
- 用对话记忆替代结构化文件。
- 把领域知识包当成事实来源。
- 手动搜索后假装是脚本采集结果。
- 在 `approval.status=pending_user_review` 时继续采集。
- 用 agent 自己的判断替用户批准采集计划。
- 来源文件 frontmatter 的 `domain` 与请求里的 `domain` 冲突时继续采集。
- 未展示 `budget_review` 的预计查询数、预计页面数和成本等级就批准采集。
- 未经用户确认就替用户选择更高或更低研究深度。

如果脚本无法完成，必须输出失败原因、缺失依赖或需要人工补充的字段，不得继续下游流程。

## 审核与自动模式

默认模式是 `review_first`。在该模式下：

- `plan_research_collection.py` 只生成采集计划，不直接采集。
- `collection_plan.research_depth_decision` 必须列出 `quick`、`standard`、`deep` 三档差异。
- 如果用户未指定 `research_depth`，默认建议 `standard`，但必须等待用户确认。
- `collection_plan.approval.status` 默认为 `pending_user_review`。
- `collection_plan.budget_review` 必须展示预计任务数、搜索词数、页面上限和成本等级。
- `collect_research_pack.py` 会拒绝未批准计划。
- 批准计划必须来自用户在当前对话中的明确确认；agent 不得用“测试批准”“默认批准”替代。

只有当请求显式包含：

```json
{
  "autonomy_mode": "auto",
  "maturity": "stable"
}
```

才允许计划自动通过，进入黑箱采集。当前流程未稳定前，不要默认使用自动模式。

## 工作模式

本技能有三种模式：

- `analyze_collection_need`：采集需求分析。用于把宽泛输入转成可执行 `collection_plan`。
- `initial_pack`：首次研究。用于建立对象的基础资料包。
- `targeted_supplement`：定向补采。用于选题、大纲或写作阶段发现关键证据缺口后，按明确问题补采资料。

`targeted_supplement` 不是重新研究一遍对象，而是围绕一个缺口做小范围、高相关度搜索。

例如：

- 首次研究发现方大同的专辑脉络完整，但缺少普通大众熟悉的歌曲入口。
- 选题阶段如果要判断“方大同为什么被普通听众记住”，就不能继续用专辑材料硬推。
- 此时应发起 `targeted_supplement`，只补采代表歌曲、平台热门曲目、歌曲评论和传播记忆。

## 目标

为指定对象生成一个可追溯、可复核、可交给后续流程使用的研究资料包，并明确哪些材料是事实、哪些是观点、哪些只能作为接受度或情绪参考。

## 阶段停点

当本技能用于测试、演示、阶段验证或用户只给出一个具体对象时，输出研究资料包后必须停下，等待用户确认。

- 可以提示下一步可进入 `topic-planner`。
- 不得在同一轮继续生成选题、大纲或正文。
- 不得因为已经加载领域知识包，就顺手完成后续判断。
- 如果用户明确说“连续执行完整流程”，才允许交给下一阶段；否则默认停在研究资料包。

## 输入

- `task_object`：研究目标。通常是具体对象、话题或选题方向，不要只写“研究一下”。
- `domain`：领域。用户已给出或上下文明显时必须保留；只有确实无法判断时才标记 unknown 并先判断领域。
- `user_limits`：用户限制，例如不要八卦、不要营销稿、不要只查中文资料。
- `research_depth`：quick、standard、deep；默认 standard。
- `autonomy_mode`：`review_first` 或 `auto`；默认 `review_first`。
- `maturity`：`experimental` 或 `stable`；默认 `experimental`。
- `source_constraints`：可选，指定必须使用或排除的平台、语言、时间范围。
- `mode`：`analyze_collection_need`、`initial_pack` 或 `targeted_supplement`；默认从 `analyze_collection_need` 开始。
- `collection_plan`：当 `mode` 为 `initial_pack` 时必填，来自 `plan_research_collection.py`。
- `targeted_research_request`：当 `mode` 为 `targeted_supplement` 时必填，来自上游阶段的补采请求。

`targeted_research_request` 应包含：

```json
{
  "request_id": "stable_request_id",
  "triggered_by": "topic-planner/outline-planner/writer/reviewer",
  "missing_decision": "当前无法判断什么",
  "target_questions": [],
  "source_types": [],
  "search_queries": [],
  "seed_urls": [],
  "collection_limit": {},
  "expected_output": []
}
```

## 研究深度

`research_depth` 会影响搜索范围、交叉验证强度和输出细节：

- `quick`
  - 目标：快速建立基本认知。
  - 来源：3-5 个可靠来源。
  - 输出：基础定义、关键事实、主要时间线、明显风险。
  - 不做：大规模评论采集、长时间历史追溯、复杂观点对照。
- `standard`
  - 目标：支撑一次选题和大纲判断。
  - 来源：6-12 个来源，至少包含事实来源和观点/评论来源。
  - 输出：时间线、阶段划分、代表材料、常见误读、可用材料、风险材料。
  - 可做：领域特定渠道的少量接受度样本。
- `deep`
  - 目标：支撑深度选题、长稿或专题。
  - 来源：12 个以上来源，多语种或多平台交叉验证。
  - 输出：多阶段材料包、争议对照、引用索引、资料缺口、可复核证据链。
  - 可做：领域特定渠道的系统性样本采集，但必须单独标注为接受度/评论材料。

## 领域知识包解析

如果输入来自已有材料文件，例如爆款拆解 Markdown，`plan_research_collection.py` 会读取 `source_constraints.seed_source` 的 frontmatter `domain`。

- 如果请求没有显式 `domain`，优先使用来源文件 `domain`。
- 如果请求显式 `domain` 与来源文件 `domain` 不一致，输出 `domain_conflict`，审查失败，不得批准采集。
- 发生 `domain_conflict` 时，不得加载任何领域特定采集任务，避免错误领域污染计划。
- 例如来源是 `domain: 泛科普`，请求却写 `music_entertainment`，必须先让用户确认领域，不能继续采集。

如果需要领域判断，按以下顺序查找领域知识包：

1. 如果调用方显式传入 `foundation_skill` 或 `foundation_path`，优先使用。
2. 读取 `.agents/skills/domain-foundation-registry.json`，按 `domain` 或 aliases 查找。
3. 如果注册表没有命中，按命名约定查找 `.agents/skills/<domain>-foundation`。
4. 如果仍未找到，不停止研究；输出 `foundation_gap`，说明缺少领域知识包，只做通用资料整理。

新增领域时，不要修改本技能正文；只需要新增领域知识包并登记到注册表。

## 通用资料来源顺序

1. 官方来源：官网、机构页、产品页、公告、论文、公开档案。
2. 权威资料库：百科、数据库、行业资料库、出版记录。
3. 主流媒体和专业媒体：采访、报道、评论、专题。
4. 一手材料：作品文本、演讲、访谈、财报、论文、作品页面。
5. 用户评论或社区讨论：只能作为接受度、情绪、争议感知参考，不作事实来源。

## 领域特定渠道

领域知识包可以补充渠道，但必须区分事实和接受度。

音乐娱乐领域示例：

- 网易云音乐：适合采集对应歌曲/专辑下的评论，用于听众记忆、情绪入口、传播场景和接受度判断。
- 豆瓣音乐/电影：适合采集专辑长评、短评、音乐纪录片/传记片评论，用于观点结构和观众判断。
- 唱片公司、流媒体平台、艺人官方渠道：适合作为发行、曲目、制作信息等事实来源。

这些渠道是音乐娱乐领域适配，不是通用研究默认步骤。

## 执行步骤

1. 将用户输入写成请求 JSON。
2. 执行 `scripts/plan_research_collection.py` 生成 `collection_plan`。
3. 执行 `scripts/validate_research_output.py --kind collection_plan`。
4. 如果审查失败，修正请求或计划，不得继续采集。
5. 执行 `scripts/collect_research_pack.py` 生成 `research_pack`。
6. 执行 `scripts/validate_research_output.py --kind research_pack`。
7. 如果审查失败，补采或修正，不得进入选题、大纲或写作。
8. 审查通过后，agent 才能基于 `evidence_by_task` 和 `sources` 填写摘要字段。
9. 输出资料包，不生成选题、大纲或正文。
10. 在测试或阶段验证场景下，输出 `next_allowed_steps`，然后停止。

## 采集需求分析脚本

`scripts/plan_research_collection.py`

职责：

- 接收宽泛输入：选题、人物、作品、话题、主题、思路方向、抽象概念。
- 判断 `object_type` 和 `domain`。
- 生成事实层、观点层、接受度层的采集任务。
- 根据 `research_depth` 固定采集规模。
- 根据领域知识包注入领域特定采集方向。
- 输出 `research_depth_decision`，提醒用户确认研究深度。
- 输出 `review_gate` 和 `approval`，控制是否允许进入采集。

推荐调用：

```bash
python .agents/skills/research-collector/scripts/plan_research_collection.py ^
  --request work/research_requests/request.json ^
  --out outputs/research_plans/request.collection_plan.json
```

`collection_plan` 必须包含：

- `plan_id`
- `task_object`
- `object_type`
- `domain`
- `collection_strategy`
- `collection_tasks`
- `research_depth_decision`
- `review_gate`
- `approval`
- `expected_research_pack_fields`
- `forbidden_outputs`

## 批准采集计划脚本

`scripts/approve_collection_plan.py`

用户审核计划后，使用该脚本记录批准。

```bash
python .agents/skills/research-collector/scripts/approve_collection_plan.py ^
  --plan outputs/research_plans/request.collection_plan.json ^
  --out outputs/research_plans/request.collection_plan.approved.json ^
  --reviewer user ^
  --note "确认 standard 深度和当前采集任务" ^
  --human-review-ack
```

如果用户要修改研究深度或采集任务，不要直接批准；应修改请求后重新生成 `collection_plan`。

`approve_collection_plan.py` 会拒绝：

- 未带 `--human-review-ack` 的批准。
- 存在 `domain_conflict` 的计划。
- 没有采集任务的计划。

## 首次研究包采集脚本

`scripts/collect_research_pack.py`

职责：

- 读取 `collection_plan`。
- 按 `collection_tasks` 逐项采集。
- 将来源分为事实、观点、接受度等层。
- 保留 `evidence_by_task`，不覆盖原始证据。
- 输出原始 `research_pack`。

推荐调用：

```bash
python .agents/skills/research-collector/scripts/collect_research_pack.py ^
  --plan outputs/research_plans/request.collection_plan.approved.json ^
  --out outputs/research_packs/request.research_pack.json ^
  --out-md outputs/research_packs/request.research_pack.md ^
  --search-backend auto
```

## 合规审查脚本

`scripts/validate_research_output.py`

每个结构化输出都必须审查：

```bash
python .agents/skills/research-collector/scripts/validate_research_output.py ^
  --kind collection_plan ^
  --input outputs/research_plans/request.collection_plan.json ^
  --out outputs/research_plans/request.collection_plan.review.json

python .agents/skills/research-collector/scripts/validate_research_output.py ^
  --kind research_pack ^
  --input outputs/research_packs/request.research_pack.json ^
  --out outputs/research_packs/request.research_pack.review.json
```

审查失败时：

- 不得进入 `topic-planner`。
- 不得写大纲。
- 不得写正文。
- 必须修正输入、计划或采集结果后重跑审查。

## 定向补采执行步骤

当 `mode` 为 `targeted_supplement` 时，按以下步骤执行：

1. 读取 `targeted_research_request.missing_decision`，明确这次补采要帮助上游判断什么。
2. 只围绕 `target_questions` 搜索，不扩展成完整人物、作品或概念研究。
3. 优先使用 `source_types` 中指定的来源类型。
4. 严格遵守 `collection_limit`，避免重新做全量采集。
5. 输出 `research_patch`，只回答本次缺口，不覆盖原始 `research_pack`。
6. 标注本次补采能解决什么、仍不能解决什么。
7. 补采完成后停止，交回触发它的上游阶段。

## 定向补采脚本

本技能包含本地脚本：

`scripts/collect_targeted_research.py`

当 `targeted_research_request` 已经明确，并且任务是网页资料补采时，优先使用该脚本执行固定采集，避免 agent 一条条手动搜索。

依赖：

- Python 3。
- 推荐安装 `ddgs`：`python -m pip install ddgs`。
- 未安装 `ddgs` 时，脚本会降级到 Bing HTML 搜索；如果搜索结果为空，应在 `still_missing` 中标注搜索后端不足，不要假装已完成补采。

脚本职责：

- 读取补采请求 JSON。
- 根据 `search_queries` 搜索；如果缺失，则由 `task_object + target_questions` 生成搜索词。
- 抓取 `seed_urls` 和搜索结果页。
- 抽取每个 `target_question` 对应的证据片段。
- 输出标准 `research_patch` JSON 和可选 Markdown 预览。

`collection_limit` 可包含：

- `max_search_results_per_query`
- `max_pages`
- `max_chars_per_page`
- `timeout_seconds`
- `allowed_domains`：只允许这些域名，适合固定平台采集。
- `blocked_domains`：排除低价值或噪声域名，例如社交主页、图片站、广告站。

脚本不负责：

- 判断选题是否成立。
- 把证据改写成文章。
- 自动合并原始 `research_pack`。
- 判断网友评论是否为事实。

推荐调用：

```bash
python .agents/skills/research-collector/scripts/collect_targeted_research.py ^
  --request work/targeted_requests/request.json ^
  --out outputs/research_patches/request.patch.json ^
  --out-md outputs/research_patches/request.patch.md ^
  --search-backend auto
```

脚本输出后，agent 必须读取 `research_patch`，再综合填写：

- `new_findings`
- `usable_for_decision`
- `still_missing`

然后交回触发它的上游阶段。

## 输出

```json
{
  "task_object": "研究对象",
  "object_type": "人物/作品/产品/组织/地点/事件/抽象话题/混合对象",
  "mode": "initial_pack",
  "domain": "domain_id_or_unknown",
  "research_depth": "standard",
  "foundation_status": {
    "loaded": true,
    "domain": "music_entertainment",
    "skill_name": "music-entertainment-foundation",
    "foundation_gap": null
  },
  "summary": "一句话说明研究对象",
  "timeline_or_evolution": [],
  "key_facts": [],
  "representative_materials": [],
  "main_viewpoints": [],
  "common_misreadings": [],
  "usable_materials": [],
  "background_only_materials": [],
  "high_risk_materials": [],
  "audience_or_public_reception": [],
  "sources": [],
  "research_gaps": [],
  "domain_notes": [],
  "next_allowed_steps": [
    "等待用户确认资料包",
    "确认后可进入 topic-planner"
  ]
}
```

## 定向补采输出

```json
{
  "task_object": "研究对象",
  "mode": "targeted_supplement",
  "triggered_by": "topic-planner",
  "missing_decision": "当前无法判断什么",
  "target_questions": [],
  "research_patch": {
    "new_findings": [],
    "evidence_by_question": [],
    "usable_for_decision": [],
    "still_missing": [],
    "sources": []
  },
  "merge_instruction": "append_only，不覆盖原 research_pack",
  "return_to": "topic-planner",
  "next_allowed_steps": [
    "等待上游阶段重新判断",
    "不得直接生成选题、大纲或正文"
  ]
}
```

## 合格检查

- 通过：具体对象和抽象话题都能生成资料包。
- 通过：`research_depth` 影响了来源数量、验证强度和输出细节。
- 通过：事实、观点、评论/接受度分开。
- 通过：领域知识包缺失时有 `foundation_gap`，而不是硬编领域规则。
- 警告：只有百科资料，缺少一手材料或专业评论。
- 失败：直接生成选题、标题、大纲或正文。
- 失败：把网友评论当成事实来源。
- 失败：测试场景下没有等待用户确认，就继续进入选题或大纲。
- 失败：定向补采时重新做全量研究，或补采内容偏离 `missing_decision`。

## 禁止事项

- 不生成选题。
- 不写大纲。
- 不写正文。
- 不把领域知识包当事实来源。
- 不把某个领域的渠道当成所有领域的默认渠道。

## 失败处理

- 无法联网或资料不足：输出 `research_gap`，列出缺失资料类型。
- 对象歧义：先列出可能对象并请求确认。
- 资料冲突：保留冲突、标注来源，不自行断言。
- 领域知识包缺失：继续通用研究，但标记 `foundation_gap`。

## 多领域适配

本技能流程通用。领域差异通过领域知识包和注册表注入；不要为每个领域复制一套研究流程。
