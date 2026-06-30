---
name: creation-task-card
description: 创作链路的原子 skill：为指定 topic 填写写手任务卡。用于“生成写手任务卡”“派写手前准备 prompt”。读取 templates/写手_dispatch.md，填入账号、领域、mode、brief、beats、draft 和篇幅参数；不写具体创作方法。
---

# 写手任务卡

这一步把本次任务的参数填好，交给写手或下一拍使用。任务卡是数据，不是写作指导。

## 输入

- `topic_id`
- `brief.md` 路径
- 账号、人设、领域、目标篇幅
- `source_type`

## 步骤

1. 读取 `templates/写手_dispatch.md`。
2. 确认 `data/topics/<id>/brief.md` 存在。
3. 创建或确认 `data/topics/<id>/beats/`。
4. 设置草稿路径为 `data/topics/<id>/draft_v1.md`。
5. 根据 `source_type` 填 mode：`hit` 为洗稿，其余默认原创。
6. 替换所有 `{{占位}}`，输出完整任务卡文本。

## 输出

- 推荐落盘：`data/topics/<id>/writer_dispatch.md`
- 或直接把完整任务卡交给写手子 agent

## 验收

- 所有 `{{...}}` 占位符都已消失。
- `brief路径`、`beats目录`、`草稿路径` 都是实际路径。
- 任务卡不包含“怎么写”的方法论细节，只包含本次任务参数。

## 禁止

- 不在任务卡里塞钩子、大纲、结构建议。
- 不替写手选打法。
- 不进入对齐拍。
