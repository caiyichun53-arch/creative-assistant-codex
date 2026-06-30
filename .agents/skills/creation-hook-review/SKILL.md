---
name: creation-hook-review
description: 创作链路的原子 skill：执行钩子审核。用于“审核钩子”“判断 3 个开头能不能放行”。读取 beats/钩子.md、brief 和 .claude/skills/钩子审核/SKILL.md，输出逐个 PASS/WARN/FAIL 和打回意见；不改写钩子。
---

# 钩子审核

这是钩子拍之后、终选之前的独立闸。

## 权威手册

读取 `.claude\skills\钩子审核\SKILL.md`。

## 输入

- `beats/钩子.md`
- `brief.md`
- 选题标题和研究材料

## 步骤

1. 逐个读取候选开头。
2. 按手册判 PASS/WARN/FAIL。
3. FAIL 必须引用原句并给出具体打回原因。
4. 若推荐项或全部候选 FAIL，打回钩子拍。
5. 若有干净候选，把可终选候选交给主编/用户。

## 输出

- 审核结果，可写入 `beats/钩子审核.md`

## 验收

- 每个候选都有判定。
- FAIL 有证据和改法。
- 带硬伤的钩子没有放行。

## 禁止

- 不亲自改钩子。
- 不用“像不像范例句式”当主要判据。
- 不跳过用户终选。
