---
name: creation-save-draft
description: 创作链路的原子 skill：登记最终 AI 草稿。用于“保存 draft_v1”“登记成稿”。调用 save_draft.py 并显式指定 author ai 和 version 1；只登记已通过审核、禁词和 humanizer 的稿件。
---

# 登记草稿

这是创作链路最后一个确定性步骤，只负责登记，不改稿。

## 输入

- `topic_id`
- 已通过的 `data/topics/<id>/draft_v1.md`

## 步骤

1. 确认 `draft_v1.md` 存在。
2. 确认篇级审稿、禁词校验、humanizer 已通过。
3. 运行 `python scripts/content/save_draft.py <id> --author ai --version 1`。
4. 返回保存路径或数据库登记结果。

## 输出

- `drafts` 表记录
- 最终稿路径

## 验收

- 必须显式使用 `--version 1`。
- 登记的是 `draft_v1.md`，不是残留的更高版本文件。

## 禁止

- 不在审核失败时登记。
- 不生成新稿。
- 不省略 `--version 1`。
