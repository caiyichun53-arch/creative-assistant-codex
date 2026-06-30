---
name: creation-banned-check
description: 创作链路的原子 skill：执行禁词和风险词校验。用于“检查 draft_v1 禁词”“跑 check_banned”。调用 scripts/content/check_banned.py，读取指定 topic 或文件，输出 RED/WARN/HINT；RED 必须返修。
---

# 禁词校验

这是确定性脚本步骤，用来发现 RED/WARN/HINT 风险。

## 输入

- `topic_id`，或
- `--file <path>`

## 步骤

1. 对 topic 运行：`python scripts/content/check_banned.py <id>`。
2. 对文件运行：`python scripts/content/check_banned.py --file <path>`。
3. 记录 RED/WARN/HINT。
4. RED 退回精修或成稿，不能放行。
5. WARN 由主编决定是否返修。

## 输出

- 终端校验结果
- 可选落盘到 `beats/禁词校验.md`

## 验收

- RED 数量为 0。
- 如果有 WARN，最终交付说明是否接受。

## 禁止

- 不靠模型猜禁词。
- 不把禁词清单提前塞进写手创作 prompt。
- 不在 RED 存在时登记草稿。
