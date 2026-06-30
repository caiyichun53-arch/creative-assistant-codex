---
name: creation-draft
description: 创作链路的原子 skill：执行“成稿”拍。用于“根据大纲写 draft_v1”“生成初稿”。读取任务卡、brief、beats/对齐.md、beats/钩子.md、beats/大纲.md 和 .claude/skills/成稿/SKILL.md，写入 topic 目录下的 draft_v1.md。
---

# 成稿拍

这一拍把已选钩子和大纲写成完整口播稿。

## 权威手册

读取 `.claude\skills\成稿\SKILL.md`。

## 输入

- 任务卡
- `brief.md`
- `beats/对齐.md`
- `beats/钩子.md` 的【选定】钩子
- `beats/大纲.md`

## 步骤

1. 读取所有上游产物。
2. 按成稿手册写完整口播稿。
3. 控制在任务卡目标篇幅范围内。
4. 写入 `data/topics/<id>/draft_v1.md`。
5. 停下，进入审稿闸。

## 输出

- `data/topics/<id>/draft_v1.md`

## 验收

- 正文只包含能说出来的话。
- 结构兑现大纲。
- 开头使用选定钩子。
- 每段有信息推进。

## 禁止

- 不临时换钩子或重排大纲，除非先回到对应拍。
- 不写分镜、字幕、BGM。
- 不自检 AI 味或禁词。
