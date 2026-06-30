---
name: outline-planner
description: 通用大纲路径技能。用于选题已确定后，结合研究资料包和领域知识包拆出段落路径、每段任务、所需材料和失败风险。不重新生成选题、不搜集资料、不写完整正文。
---

# 通用大纲路径

## 使用时机

当已经有一个确定选题，并且有研究资料包时使用。

如果只有对象没有选题，先执行 `research-collector` 和 `topic-planner`。

## 目标

把已确定选题拆成可写作的段落路径，并明确每段解决的问题、使用的材料、绑定的领域判断和失败风险。

## 阶段停点

当本技能用于测试、演示或阶段验证时，输出大纲路径后必须停下，等待用户确认。

- 不得把大纲直接扩写成正文。
- 不得在大纲阶段重新选题。
- 不得为了补足段落而临时编造资料。
- 只有用户明确要求“开始写稿”或调用写作技能，才允许进入写作阶段。

## 输入

- `selected_topic`：已确定选题。它应来自用户明确指定，或来自 `topic-planner` 的推荐结果。
- `research_pack`：研究资料包。
- `domain`：领域名称；通常从研究资料包读取。领域已明确时不得重新猜测。
- `target_length`：可选，短内容、中等内容、长内容。
- `user_limits`：用户限制。
- `foundation_skill` 或 `foundation_path`：可选，显式指定领域知识包。

## 领域知识包解析

按以下顺序查找领域知识包：

1. 显式输入的 `foundation_skill` 或 `foundation_path`。
2. `.agents/skills/domain-foundation-registry.json` 中与 `domain` 或 aliases 匹配的条目。
3. 命名约定 `.agents/skills/<domain>-foundation`。
4. 如果找不到，输出 `foundation_gap`，只做通用大纲，不做领域判断。

## 执行步骤

1. 检查选题是否有核心问题；没有核心问题则退回选题阶段。
2. 检查研究资料包是否足够支撑该选题；不足则输出 `research_gaps`。
3. 确认领域。
   - 如果研究资料包已有明确领域，沿用它。
   - 如果领域是 unknown，退回研究或选题阶段确认领域。
4. 解析领域知识包。
5. 如果领域知识包存在，选取 3-7 个领域判断作为大纲约束。
6. 确定段落数量：
   - 短内容：4-6 段。
   - 中等内容：6-8 段。
   - 长内容：8-12 段。
7. 每段必须写清：
   - 段落任务。
   - 要解决的问题。
   - 使用材料。
   - 绑定领域判断。
   - 不能怎么写。
8. 标注需要补查的资料，不自行编造。
9. 输出大纲路径，不写正文台词。
10. 在测试或阶段验证场景下，输出 `next_allowed_steps`，然后停止。

## 输出

```json
{
  "selected_topic": "已选题目",
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
  "outline": [
    {
      "section": 1,
      "role": "开头/背景/论证/转折/案例/收束",
      "task": "这一段要完成什么",
      "question_to_answer": "这一段回答什么问题",
      "materials": [],
      "foundation_items": [],
      "must_not": "这一段不能怎么写"
    }
  ],
  "research_gaps": [],
  "overall_failure_risks": [],
  "next_allowed_steps": [
    "等待用户确认大纲",
    "确认后才进入写作阶段"
  ]
}
```

## 合格检查

- 通过：每段都有明确任务和问题。
- 通过：每段最多绑定 1-2 个领域判断。
- 通过：能指出每段不能怎么写。
- 通过：领域知识包缺失时明确标注 `foundation_gap`。
- 警告：段落过多但推进关系不清。
- 失败：重新生成多个选题。
- 失败：直接写正文。
- 失败：按资料顺序机械排列。
- 失败：测试场景下没有等待用户确认，就继续进入写作。

## 禁止事项

- 不重新选题。
- 不搜集新资料，除非只列出需要补查项。
- 不写完整正文。
- 不输出标题党包装。
- 不让背景资料独立堆成段落，除非它能解释核心问题。

## 失败处理

- 选题核心问题不清：退回 `topic-planner`。
- 研究资料不足：输出 `research_gaps`，停止进入写作。
- 领域知识包缺失：标记 `foundation_gap`，只输出通用大纲草案。

## 多领域适配

本技能的大纲格式通用。段落判断和失败边界来自领域知识包；新增领域时只新增知识包和注册表条目，不复制本技能。
