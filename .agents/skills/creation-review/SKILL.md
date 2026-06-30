---
name: creation-review
description: 创作链路的原子 skill：执行篇级审稿。用于“审稿 draft_v1”“按清单判 PASS/WARN/FAIL”。读取 draft_v1.md、brief、任务卡、beats/钩子.md 和 .claude/skills/审稿/SKILL.md，输出审稿结果和打回意见；不改稿。
---

# 篇级审稿

这是成稿后的第一道闸，只判整篇硬伤。

## 权威手册

读取 `.claude\skills\审稿\SKILL.md`。

## 输入

- `data/topics/<id>/draft_v1.md`
- `brief.md`
- 任务卡
- `beats/钩子.md`

## 步骤

1. 逐条按审稿手册判 PASS/WARN/FAIL。
2. FAIL 必须引用原文并说明具体问题。
3. 任一关键项 FAIL，整体 FAIL，退回成稿或对应上游拍。
4. 全部关键项通过后，才进入优化诊断。

## 输出

- 审稿结果，可写入 `beats/审稿.md`

## 验收

- 每条规则有判定。
- 关键 FAIL 没有放行。
- 打回意见能定位到原文句子。

## 禁止

- 不自己改稿。
- 不把句级 AI 味放到本步骤处理。
- 不把最终品味选择推给审稿清单。
