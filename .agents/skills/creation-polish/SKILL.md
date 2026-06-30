---
name: creation-polish
description: 创作链路的原子 skill：执行精修返稿。用于“根据优化意见精修 draft_v1”“把合格稿提一档”。读取 draft_v1.md、beats/优化意见.md 和 .claude/skills/精修/SKILL.md，改写并覆盖 draft_v1.md；不改选题、钩子和结构大方向。
---

# 精修拍

这一步由写手根据优化意见改稿，主编不亲自润笔。

## 权威手册

读取 `.claude\skills\精修\SKILL.md`。

## 输入

- `draft_v1.md`
- `beats/优化意见.md`
- 上游对齐、钩子、大纲产物

## 步骤

1. 读取优化意见和当前 draft。
2. 逐条落实能提升的意见。
3. 保持选定钩子、结构方向和事实边界。
4. 覆盖写回 `draft_v1.md`。
5. 停下，进入禁词和 humanizer。

## 输出

- 更新后的 `draft_v1.md`

## 验收

- 优化意见已逐条处理或说明未处理理由。
- 结构没有被擅自重做。
- 信息事实没有被新编。

## 禁止

- 不换题。
- 不重做钩子，除非退回钩子拍。
- 不处理禁词和 AI 味。
