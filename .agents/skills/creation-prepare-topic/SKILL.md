---
name: creation-prepare-topic
description: 创作链路的原子 skill：为指定 topic 生成或检查材料包。用于“备料选题 N”“生成材料包”“检查 brief 是否齐全”。负责调用 prepare_topic.py，验收 brief_full.md 与写手版 brief.md；不写文案、不做后续创作。
---

# 备料材料包

这是创作链路的第一个可独立测试步骤。目标是让 `data/topics/<id>/brief_full.md` 和写手版 `brief.md` 就位。

## 输入

- `topic_id`
- 可选：是否只检查，不重新生成

## 步骤

1. 确认 `data/creation.db` 中存在该 topic。
2. 若 `data/topics/<id>/brief.md` 不存在，运行 `python scripts/topics/prepare_topic.py <id>`。
3. 若是原创新题，先用 `python scripts/topics/new_topic.py --title ... --domain ...` 建 topic。
4. 若是衍生题，先用 `python scripts/topics/spinoff.py --from-topic <id>` 列方向，再由用户或调用方选择。
5. 只检查产物，不进入写手流程。

## 输出

- `data/topics/<id>/brief_full.md`
- `data/topics/<id>/brief.md`

## 验收

- `brief_full.md` 存在，保留规划和查重需要的信息。
- `brief.md` 存在，已剔除来源口播、金句、预写洞察、Hook、结构、可裂变选题等会抢写手判断的段落。
- 命令失败时返回失败原因，不硬编材料。

## 禁止

- 不写钩子、大纲、正文。
- 不把 `brief_full.md` 当写手唯一输入。
- 不复制大量 `data/` 或临时产物。
