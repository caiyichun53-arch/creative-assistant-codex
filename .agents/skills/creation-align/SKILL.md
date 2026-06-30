---
name: creation-align
description: 创作链路的原子 skill：执行第 0 拍“对齐”。用于“对齐选题 N”“生成可用事实清单”“确认洗稿/原创模式”。读取任务卡、brief 和 .claude/skills/对齐/SKILL.md，写入 beats/对齐.md，完成后停下等反馈。
---

# 对齐拍

这一拍只理料和确认模式，不写文案。

## 权威手册

读取 `.claude\skills\对齐\SKILL.md`。

## 输入

- 任务卡或等价参数
- `data/topics/<id>/brief.md`
- `data/topics/<id>/beats/`
- mode：洗稿或原创

## 步骤

1. 读取任务卡和 brief。
2. 按对齐手册区分洗稿/原创。
3. 只提炼事实生料，洗稿模式标注“来源已有/新增量”。
4. 写入 `data/topics/<id>/beats/对齐.md`。
5. 停下，等待主编或用户反馈。

## 输出

- `beats/对齐.md`

## 验收

- 有可用事实清单。
- 没有预写钩子、角度、立意、结构。
- mode 已确认。

## 禁止

- 不写钩子。
- 不读来源口播原文逐字来复述。
- 不把洞察包装成事实。
