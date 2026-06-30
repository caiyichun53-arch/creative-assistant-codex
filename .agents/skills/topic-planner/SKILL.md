---
name: topic-planner
description: 通用选题规划技能。用于已有研究资料包后，结合领域知识包生成多个可做选题，并评估成立理由、资料支撑、风险和推荐路径。不负责搜集资料、不写详细大纲、不写正文。
---

# 通用选题规划

## 使用时机

当研究资料包已经存在，或上一步刚完成 `research-collector` 后使用。

如果用户只给对象、没有资料包，先执行研究资料包步骤，不要直接生成选题。

## 目标

基于研究资料包和领域知识包，生成多个可执行选题，并说明每个选题为什么成立、适合什么内容类型、风险在哪里。

## 硬性原则

固定路线必须脚本化。不得让 agent 临场自由生成选题、临场决定是否补采、或凭对话记忆做增量复判。

本技能必须按以下路线执行：

1. 用 `scripts/plan_topics.py` 从已审查通过的 `research_pack` 生成 `topic_plan` 和 `topic_state`。
2. 用 `scripts/validate_topic_output.py --kind initial_plan` 审查选题结果。
3. 如果存在 `targeted_research_requests`，交给 `research-collector` 的补采脚本执行。
4. 补采结果审查通过后，用 `scripts/recheck_topics.py` 做增量复判。
5. 用 `scripts/validate_topic_output.py --kind incremental_recheck` 审查复判结果。
6. 任何审查失败时，不得进入大纲或写作。

脚本职责边界：

- 选题脚本不搜索网页。
- 选题脚本不直接产出最终成品选题，只产出“证据脚手架”：候选方向、材料支撑、缺口、风险和补采请求。
- 选题脚本不写大纲。
- 选题脚本不写正文。
- 增量复判必须读取 `topic_state`，不得重新洗牌。
- 真正的选题表达必须在脚本输出之后，由 agent 或用户基于 `topic_material_profile`、`source_task_ids` 和 `topic_development_brief` 做二次判断。

## 与资料补采的关系

本技能不执行搜索，但可以向 `research-collector` 发出定向补采请求。

在测试、演示或流程未稳定阶段，如果 `topic_plan.targeted_research_requests` 非空，必须先把补采请求展示给用户审核，不能自动执行补采。用户明确确认某个补采请求后，才允许进入 `research-collector` 的 `targeted_supplement`。

当候选选题看起来可能成立，但当前 `research_pack` 缺少关键证据时，不得硬生成完整判断，应输出 `targeted_research_request`，交回 `research-collector` 的 `targeted_supplement` 模式。

补采请求必须按缺口合并：

- 同一个 `missing_decision` 只生成一个共享补采请求。
- 共享补采请求必须包含 `related_topic_ids`，说明它会回填哪些候选方向。
- `topic_state.open_requests` 必须保存同一个请求对应的全部 `topic_ids`。
- 增量复判收到补采结果后，必须同时更新所有关联 topic，不得只更新第一个 topic。
- 只有缺口不同，才允许生成多个补采请求。
- 补采查询词必须匹配对象类型；机制题不得出现“代表作、热门歌曲、专辑”等人物/作品残留词。

触发定向补采的典型情况：

- 资料只有专辑层，缺少普通大众熟悉的歌曲入口。
- 资料只有事实时间线，缺少听众接受度或争议。
- 资料只有大众评论，缺少一手访谈或权威事实。
- 资料能支撑人物背景，但不能支撑某个具体选题的核心判断。

定向补采必须是“带问题的补采”，不是重新研究对象。

## 阶段停点

当本技能用于测试、演示或阶段验证时，输出候选选题后必须停下，等待用户选择或修改方向。

- 可以给出推荐顺序，但不得默认替用户选定最终题目并进入大纲。
- 不得把推荐选题扩写成段落路径。
- 如果用户给的是一个人物或对象，本技能只能处理已经完成的研究资料包；不能跳过研究阶段。
- 只有用户明确选择某个选题，或明确要求“继续生成大纲”，才允许进入 `outline-planner`。

## 脚本输出后的判断

`plan_topics.py` 的输出不是最终选题清单，而是让 agent 和用户审查的材料组织结果。使用时必须按下面顺序解释：

1. 先看 `topic_material_profile`：资料层是否足够，强项和弱项在哪里。
2. 再看每个候选方向的 `source_task_ids` 和 `evidence_used`：这个方向到底由哪些材料支撑。
3. 再看 `missing_evidence`：缺口是否会影响核心判断。
4. 再看 `topic_development_brief.agent_judgment_required`：agent 需要做哪些非脚本判断。
5. 最后才允许把候选方向改写成更具体的选题表达。

如果某个方向只是标题好看，但证据集中在弱层或缺口没有解决，应保留为 `needs_research`，不要强行推荐。

## 脚本

- `scripts/plan_topics.py`：输入 `research_pack`，输出 `topic_plan`、`topic_state`、`topic_material_profile`、候选方向证据脚手架、可选 `targeted_research_requests`。它不负责最终选题润色。
- `scripts/validate_topic_output.py`：审查 `initial_plan` 或 `incremental_recheck` 是否满足契约。
- `scripts/recheck_topics.py`：输入 `topic_state` 和 `research_patch`，输出增量复判结果。

推荐调用：

```bash
python .agents/skills/topic-planner/scripts/plan_topics.py ^
  --research-pack outputs/research_packs/request.research_pack.json ^
  --out outputs/topic_plans/request.topic_plan.json

python .agents/skills/topic-planner/scripts/validate_topic_output.py ^
  --kind initial_plan ^
  --input outputs/topic_plans/request.topic_plan.json ^
  --out outputs/topic_plans/request.topic_plan.review.json
```

增量复判：

```bash
python .agents/skills/topic-planner/scripts/recheck_topics.py ^
  --topic-state outputs/topic_plans/request.topic_plan.json ^
  --research-patch outputs/research_patches/request.patch.json ^
  --out outputs/topic_plans/request.topic_recheck.json

python .agents/skills/topic-planner/scripts/validate_topic_output.py ^
  --kind incremental_recheck ^
  --input outputs/topic_plans/request.topic_recheck.json ^
  --out outputs/topic_plans/request.topic_recheck.review.json
```

## 输入

- `research_pack`：研究资料包。
- `domain`：领域名称。通常应从 `research_pack.domain` 读取；如果用户或上下文已明确领域，不要重新猜测。
- `user_limits`：用户限制。
- `target_count`：候选选题数量，默认 5。
- `content_type_preference`：可选，用户偏好的内容类型。
- `foundation_skill` 或 `foundation_path`：可选，显式指定领域知识包。
- `mode`：`initial_plan` 或 `incremental_recheck`；默认 `initial_plan`。
- `topic_state`：当 `mode` 为 `incremental_recheck` 时必填，来自上一次选题规划的判断快照。
- `research_patch`：当 `mode` 为 `incremental_recheck` 时必填，来自 `research-collector` 定向补采结果。

## 选题判断状态

为了支持定向补采后的增量复判，第一次选题规划必须输出 `topic_state`。它是后续复判的判断记忆，不依赖对话上下文。

`topic_state` 必须保存：

- 每个候选选题的稳定 `topic_id`。
- 选题标题和核心问题。
- 第一次判断时使用的证据。
- 第一次判断时缺失的证据。
- 风险和失败边界。
- 分数和推荐顺序。
- 是否已发起定向补采。
- 本次判断使用过的领域知识卡或规则。

如果无法保存 `topic_state`，不得执行 `incremental_recheck`，只能重新执行 `initial_plan`，并明确说明“缺少上一轮判断状态，无法做增量复判”。

示例：

```json
{
  "state_id": "topic_state_2026-06-26_fang_datong_v1",
  "task_object": "方大同",
  "domain": "music_entertainment",
  "version": 1,
  "topics": [
    {
      "topic_id": "topic_001",
      "topic_title": "候选选题标题",
      "core_question": "这个选题真正要回答的问题",
      "status": "candidate/needs_research/strong/weak/rejected",
      "evidence_used": [],
      "missing_evidence": [],
      "risk": [],
      "score": {},
      "recommended_foundation_items": [],
      "targeted_research_request_id": null
    }
  ],
  "ranking": ["topic_001"],
  "open_requests": []
}
```

## 增量复判

当 `mode` 为 `incremental_recheck` 时，本技能不得重新洗牌式生成一套全新选题，而应基于 `topic_state` 和 `research_patch` 做复判。

执行原则：

1. 先读取 `topic_state.topics`，保留原有 `topic_id`。
2. 判断 `research_patch` 回答了哪些 `missing_evidence`。
3. 只更新受本次补采影响的候选选题。
4. 未受影响的选题保持原状态和原排序，除非新证据改变了整体推荐顺序。
5. 可以新增少量选题，但必须说明它是由 `research_patch` 直接引出的，不得借机重做全量选题。
6. 可以淘汰原选题，但必须说明是哪条新证据或仍缺失的证据导致淘汰。
7. 输出新的 `topic_state.version + 1`，保留上一版 `state_id` 作为 `based_on_state_id`。

增量复判只允许以下动作：

- `confirm`：补采证据增强原选题。
- `downgrade`：补采证据不足或削弱原选题。
- `reject`：补采证据否定原选题。
- `rerank`：调整推荐顺序。
- `add_from_patch`：由补采材料直接新增选题。
- `keep`：未受影响，保持不变。

## 定向补采请求格式

当需要补采时，输出的 `targeted_research_request` 必须能被 `research-collector/scripts/collect_targeted_research.py` 直接读取。

必须包含：

- `request_id`：稳定 ID，后续 `research_patch` 和 `topic_state.open_requests` 用它关联。
- `topic_id`：触发补采的候选选题。
- `related_topic_ids`：同一补采请求影响的所有候选选题。单一 topic 也要写成数组。
- `triggered_by`：固定为 `topic-planner`。
- `missing_decision`：当前无法判断什么。
- `target_questions`：补采要回答的问题。
- `source_types`：期望来源类型。
- `search_queries`：建议搜索词；不要只依赖脚本自动生成。
- `seed_urls`：已知高价值来源，可为空。
- `collection_limit`：限制搜索结果、页面数量、评论数量或时间范围。
- `expected_output`：希望补采返回哪些字段。

示例：

```json
{
  "request_id": "trr_001",
  "topic_id": "topic_001",
  "task_object": "方大同",
  "triggered_by": "topic-planner",
  "missing_decision": "能否证明大众入口偏情歌，但专业价值不止情歌？",
  "target_questions": [
    "大众平台入口是否主要集中在情歌和旋律性强的作品？",
    "专业/编辑向材料是否反复强调 R&B、Soul、Neo-Soul、Funk、节奏和制作？"
  ],
  "source_types": [
    "流媒体热门歌曲",
    "Apple Music 编辑歌单",
    "专业或半专业乐评"
  ],
  "search_queries": [
    "方大同 热门歌曲 特别的人 爱爱爱 三人游 Love Song",
    "方大同 R&B Soul Neo-Soul 乐评 爱爱爱",
    "Khalil Fong popular songs Spotify Apple Music"
  ],
  "seed_urls": [],
  "collection_limit": {
    "max_search_results_per_query": 5,
    "max_pages": 12,
    "max_chars_per_page": 50000,
    "blocked_domains": ["instagram.com", "facebook.com", "pinterest.com"]
  },
  "expected_output": [
    "representative_songs",
    "public_memory_evidence",
    "professional_value_evidence",
    "remaining_gaps"
  ]
}
```

## 领域知识包解析

按以下顺序查找领域知识包：

1. 显式输入的 `foundation_skill` 或 `foundation_path`。
2. `.agents/skills/domain-foundation-registry.json` 中与 `domain` 或 aliases 匹配的条目。
3. 命名约定 `.agents/skills/<domain>-foundation`。
4. 如果找不到，输出 `foundation_gap`，仍可生成通用选题，但必须标注“缺少领域判断，结果只作草案”。

新增领域时，新增领域知识包并登记注册表，不修改本技能正文。

## 执行步骤

1. 检查 `research_pack` 是否包含：
   - 基础事实。
   - 关键阶段或概念演变。
   - 代表材料。
   - 可用材料。
   - 高风险材料。
   - 资料来源。
2. 确认领域。
   - 如果 `research_pack.domain` 已明确，沿用它。
   - 如果领域是 unknown，先退回研究阶段确认领域，不要直接生成正式选题。
3. 解析领域知识包。
4. 如果领域知识包存在，从中选择 3-7 个与当前对象相关的判断维度或能力卡。
5. 生成候选选题，每个选题必须包含：
   - 标题。
   - 核心问题。
   - 内容类型。
   - 成立理由。
   - 材料支撑。
   - 资料缺口。
   - 失败风险。
   - 推荐使用的领域判断。
6. 对选题评分：
   - 材料支撑度。
   - 新鲜度。
   - 深度空间。
   - 风险可控度。
   - 与用户限制的匹配度。
7. 给出推荐顺序，不写详细大纲。
8. 在测试或阶段验证场景下，输出 `next_allowed_steps`，然后停止。

## 增量复判执行步骤

当 `mode` 为 `incremental_recheck` 时，改用以下步骤：

1. 检查 `topic_state` 是否存在且包含稳定 `topic_id`；缺失则停止，要求重新执行 `initial_plan`。
2. 检查 `research_patch` 是否来自 `targeted_supplement`；如果是全量资料包，先要求拆成补丁或明确使用范围。
3. 对照每个候选选题的 `missing_evidence` 和本次 `research_patch.evidence_by_question`。
4. 对每个受影响选题输出 `recheck_action` 和理由。
5. 更新证据、风险、分数、状态和推荐顺序。
6. 如新增选题，只能使用 `add_from_patch`，并说明来自哪条补采证据。
7. 输出新版 `topic_state`，不得丢弃上一轮判断记录。
8. 停下，等待用户确认。

## 输出

```json
{
  "task_object": "研究对象",
  "domain": "domain_id",
  "foundation_status": {
    "loaded": true,
    "skill_name": "music-entertainment-foundation",
    "foundation_gap": null
  },
  "foundation_use": {
    "selected_cards_or_rules": [],
    "selection_reason": []
  },
  "mode": "initial_plan",
  "topics": [
    {
      "topic_title": "候选选题标题",
      "content_type": "内容类型",
      "core_question": "这个选题真正要回答的问题",
      "why_it_works": [],
      "material_support": [],
      "research_gaps": [],
      "risk": [],
      "recommended_foundation_items": [],
      "score": {
        "material_support": 4,
        "freshness": 3,
        "depth": 4,
        "risk_control": 4,
        "user_fit": 5
      }
    }
  ],
  "topic_state": {
    "state_id": "topic_state_id",
    "task_object": "研究对象",
    "domain": "domain_id",
    "version": 1,
    "topics": [],
    "ranking": [],
    "open_requests": []
  },
  "recommended_topic": "推荐选题标题",
  "targeted_research_requests": [
    {
      "topic_title": "需要补采的候选选题",
      "triggered_by": "topic-planner",
      "missing_decision": "当前无法判断什么",
      "target_questions": [],
      "source_types": [],
      "search_queries": [],
      "seed_urls": [],
      "collection_limit": {},
      "expected_output": []
    }
  ],
  "next_allowed_steps": [
    "等待用户选择或修改选题",
    "确认后可进入 outline-planner"
  ]
}
```

## 增量复判输出

```json
{
  "mode": "incremental_recheck",
  "based_on_state_id": "topic_state_2026-06-26_fang_datong_v1",
  "new_state_id": "topic_state_2026-06-26_fang_datong_v2",
  "research_patch_used": "targeted_supplement_result_id",
  "recheck_results": [
    {
      "topic_id": "topic_001",
      "recheck_action": "confirm/downgrade/reject/rerank/add_from_patch/keep",
      "reason": "为什么这样更新",
      "evidence_added": [],
      "remaining_gaps": [],
      "score_change": {}
    }
  ],
  "topic_state": {
    "state_id": "topic_state_2026-06-26_fang_datong_v2",
    "based_on_state_id": "topic_state_2026-06-26_fang_datong_v1",
    "task_object": "方大同",
    "domain": "music_entertainment",
    "version": 2,
    "topics": [],
    "ranking": [],
    "open_requests": []
  },
  "next_allowed_steps": [
    "等待用户确认复判结果",
    "确认后可选择某个选题进入 outline-planner"
  ]
}
```

## 合格检查

- 通过：每个选题都有核心问题，不只是标题。
- 通过：每个选题都能指出资料支撑和失败风险。
- 通过：领域知识包缺失时明确标注 `foundation_gap`。
- 警告：选题之间只是措辞不同，核心问题重复。
- 失败：没有研究资料包就直接生成选题。
- 失败：输出完整大纲或正文。
- 失败：测试场景下没有等待用户确认，就继续进入大纲或写作。
- 失败：关键证据缺失时继续硬推选题，而不是输出 `targeted_research_request`。
- 失败：执行增量复判时没有读取 `topic_state`，或丢弃上一轮 `topic_id` 重新洗牌。

## 禁止事项

- 不执行资料搜集。
- 不写详细段落路径。
- 不写正文。
- 不用爆款标题替代选题判断。
- 不把领域知识包当模板套题。

## 失败处理

- 研究资料包不足：输出 `need_research`，列出必须补查的资料。
- 领域知识包缺失：输出 `foundation_gap`，选题结果只标为草案。
- 所有候选选题都依赖高风险材料：输出 `topic_not_ready`。
- 用户指定内容类型与材料不匹配：说明原因并给出更稳的内容类型。

## 多领域适配

本技能的选题结构通用。领域判断来自领域知识包；新增领域时只新增知识包和注册表条目，不复制本技能。
